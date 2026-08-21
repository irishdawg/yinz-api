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
from gotiate.api.schemas import CommandRequest, CreateGameRequest, GameSummary, JoinGameRequest, PlayerNameOffer, ThemeSetSummary
from gotiate.domain import engine, themes
from gotiate.domain.entities import CommandReceipt, CommandStatus, Game, GameConfig
from gotiate.domain.errors import DomainError, IllegalCommandError, StaleVersionError
from gotiate.domain.errors import NotFoundError as DomainNotFoundError
from gotiate.domain.projections import PlayerAudience, PublicAudience, project, project_events
from gotiate.persistence.player_names import NoAvailableNameError, PlayerNameRepository
from gotiate.persistence.repository import GameRepository

_INVALID_NAME_DETAIL = {"error_code": "invalid_display_name", "message": "enter a name (up to 24 characters) or choose one from the list"}
_NO_NAMES_AVAILABLE_DETAIL = {"error_code": "no_names_available", "message": "try again in a moment"}
_UNKNOWN_THEME_SET_DETAIL = {"error_code": "unknown_theme_set", "message": "choose one of the available theme sets"}

# In-person play wants real/familiar names, not "wait, who's Mortia?" --
# see the real-names design writeup. A submitted display_name that's a
# recognized player_name_seeds entry still goes through the original
# catalog path (golden-eligible, usage-tallied); anything else is now
# accepted as a typed name, subject only to this structural check --
# never a content filter, that's explicitly deferred (see CURRENT_WORK.md).
_MAX_TYPED_NAME_LENGTH = 24

router = APIRouter(prefix="/games", tags=["games"])
player_names_router = APIRouter(prefix="/player-names", tags=["player-names"])
theme_sets_router = APIRouter(prefix="/theme-sets", tags=["theme-sets"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _resolve_submitted_name(name_repo: PlayerNameRepository, raw_name: str) -> tuple[str, bool]:
    """Returns (display_name, is_catalog_name), uppercased either way --
    name case is standardized to upper across the board, catalog and typed
    alike (see player_names.py's own module docstring for how the catalog
    -matching methods stay case-insensitive under this). A catalog name is
    still eligible for the golden-name roll (see roll_golden_name's
    docstring). Anything else is a typed name: trimmed, structurally
    checked (non-empty, length-capped), and *never* golden-eligible --
    overwriting a real person's typed name with a random catalog string on
    a golden hit would defeat the entire point of typing it. Raises the
    same 422 either way so the client doesn't need to distinguish the two
    failure modes."""
    if await name_repo.is_valid_name(raw_name):
        return raw_name.upper(), True
    typed_name = raw_name.strip()
    if not typed_name or len(typed_name) > _MAX_TYPED_NAME_LENGTH:
        raise HTTPException(status_code=422, detail=_INVALID_NAME_DETAIL)
    return typed_name.upper(), False


@router.post("", response_model=GameSummary)
@limiter.limit("5/hour")
async def create_game(
    request: Request,
    body: CreateGameRequest,
    auth_user_id: str = Depends(get_auth_user_id),
    repo: GameRepository = Depends(get_repository),
    name_repo: PlayerNameRepository = Depends(get_player_name_repository),
) -> GameSummary:
    display_name, is_catalog_name = await _resolve_submitted_name(name_repo, body.display_name)

    # Validated here, eagerly, so a bad theme_set_id 422s immediately —
    # engine.create_game would otherwise happily store it and only fail
    # later at START_GAME (themes are resolved lazily there today).
    config = None
    if body.theme_set_id is not None:
        try:
            themes.get_theme_set(body.theme_set_id)
        except DomainNotFoundError:
            raise HTTPException(status_code=422, detail=_UNKNOWN_THEME_SET_DETAIL)
        config = GameConfig(theme_set_id=body.theme_set_id)

    # One in-flight hosted game per user — deliberately not just a time-
    # windowed rate limit, so spacing requests out doesn't get around it.
    existing_game = await repo.find_active_game_hosted_by(auth_user_id)
    if existing_game is not None:
        raise HTTPException(
            status_code=409,
            detail={"error_code": "active_game_exists", "message": "finish or end your current game before starting another", "game_id": existing_game.id},
        )

    # Golden odds rolled here, once, at the moment a real seat is actually
    # created -- never on the free-preview offer endpoints (see
    # player_names.roll_golden_name), and never for a typed name (see
    # _resolve_submitted_name). Overrides the previewed display_name
    # outright rather than just flagging it, so "golden" always means a
    # real golden name, not an arbitrary chosen one wearing a badge.
    is_golden_name = False
    if is_catalog_name:
        golden_name = await name_repo.roll_golden_name()
        is_golden_name = golden_name is not None
        if golden_name is not None:
            display_name = golden_name

    game, events = engine.create_game(
        actor_auth_user_id=auth_user_id,
        display_name=display_name,
        now=_now(),
        config=config,
        is_golden_name=is_golden_name,
    )
    # No existing row to lock for a brand-new game id -- lock_for() here is
    # supplying the same atomic transaction context every other mutating
    # route gets (create + append_events land together or not at all), not
    # protecting a row that doesn't exist yet.
    async with repo.lock_for(game.id):
        await repo.create(game)
        await repo.append_events(events)
    # usage_count is a tally over the curated pool specifically -- a typed
    # name was never offered from it, so there's nothing to tally.
    if is_catalog_name:
        await name_repo.mark_name_used(display_name)
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
    display_name, is_catalog_name = await _resolve_submitted_name(name_repo, body.display_name)

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
        # who's using what name in which game right now. Checked against
        # the resolved (trimmed) name, before any golden override below --
        # source-agnostic, so two players typing the same name collide
        # exactly like two players picking the same catalog name always did.
        existing_names = {p.display_name for p in game.players}
        if display_name in existing_names:
            raise HTTPException(status_code=409, detail={"error_code": "name_taken", "message": "that name is already taken in this game — pick another"})

        # Same seat-bound golden roll as create_game -- see the comment
        # there, including never rolling for a typed name. `existing_names`
        # also keeps this game's own golden roll (if any) from colliding
        # with a golden name someone else in this same lobby already landed.
        is_golden_name = False
        if is_catalog_name:
            golden_name = await name_repo.roll_golden_name(exclude=existing_names)
            is_golden_name = golden_name is not None
            if golden_name is not None:
                display_name = golden_name

        try:
            player, events = engine.join_game(game, actor_auth_user_id=auth_user_id, display_name=display_name, now=_now(), is_golden_name=is_golden_name)
        except DomainError as exc:
            raise HTTPException(status_code=409, detail={"error_code": exc.error_code, "message": str(exc)}) from exc
        await repo.save(game)
        await repo.append_events(events)
    if is_catalog_name:
        await name_repo.mark_name_used(display_name)

    return GameSummary(game_id=game.id, join_code=game.join_code, game_player_id=player.game_player_id)


async def _sync_due_time_transitions(repo: GameRepository, game: Game, now: datetime) -> Game:
    """GET is otherwise a pure read, but a time-based phase transition
    (negotiation clock elapsing, lobby grace elapsing) can't happen until
    *something* touches this game -- once the actor who'd otherwise submit
    a command has gone quiet (an abandoned lobby, an idle negotiation),
    polling is the only thing left that ever will. `is_time_transition_due`
    is a cheap, lock-free check so this doesn't add lock/write overhead to
    every ordinary poll -- only when something's genuinely due does it
    acquire the lock, re-fetch, and persist for real."""
    if not engine.is_time_transition_due(game, now):
        return game
    async with repo.lock_for(game.id):
        locked_game = await repo.get(game.id)
        if locked_game is None:
            return game
        events = engine.apply_due_time_transitions(locked_game, now)
        if events:
            locked_game.version += 1
            await repo.save(locked_game)
            await repo.append_events(events)
        return locked_game


@router.get("/active")
async def get_active_game(
    auth_user_id: str = Depends(get_auth_user_id),
    repo: GameRepository = Depends(get_repository),
) -> dict:
    """Backs the mobile sleep/re-entry fix: a returning session with an
    existing active seat (any phase, not just LOBBY) routes straight back
    into that game. Registered before GET /{game_id} -- a literal path
    segment must win over the catch-all parametrized route, same reasoning
    as /games/join already needing to precede it."""
    game = await repo.find_active_game_seated_in(auth_user_id)
    if game is None:
        raise HTTPException(status_code=404, detail="no active game")
    return {"game_id": game.id, "join_code": game.join_code}


@router.get("/{game_id}")
async def get_game(
    game_id: str,
    auth_user_id: str = Depends(get_auth_user_id),
    repo: GameRepository = Depends(get_repository),
) -> dict:
    game = await repo.get(game_id)
    if game is None:
        raise HTTPException(status_code=404, detail="no such game")
    game = await _sync_due_time_transitions(repo, game, _now())
    player = game.player_by_auth_id(auth_user_id)
    audience = PlayerAudience(player.game_player_id) if player else PublicAudience()
    return project(game, audience)


@router.get("/{game_id}/events")
async def get_game_events(
    game_id: str,
    since_seq: int = 0,
    auth_user_id: str = Depends(get_auth_user_id),
    repo: GameRepository = Depends(get_repository),
) -> list[dict]:
    game = await repo.get(game_id)
    if game is None:
        raise HTTPException(status_code=404, detail="no such game")
    player = game.player_by_auth_id(auth_user_id)
    audience = PlayerAudience(player.game_player_id) if player else PublicAudience()
    events = await repo.get_events(game_id, since_seq)
    return project_events(game, events, audience)


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
        # since it's the same class of staleness (a player's own mutable
        # state could equally be stale between the two reads, even though
        # today's 403 check below doesn't depend on it).
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


@theme_sets_router.get("", response_model=list[ThemeSetSummary])
async def list_theme_sets(auth_user_id: str = Depends(get_auth_user_id)) -> list[ThemeSetSummary]:
    repo = themes.get_theme_repository()
    return [ThemeSetSummary(theme_set_id=tid, name=repo.get(tid).name) for tid in repo.list_ids()]


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
async def offer_initial_name(
    request: Request,
    join_code: str | None = None,
    auth_user_id: str = Depends(get_auth_user_id),
    repo: GameRepository = Depends(get_repository),
    name_repo: PlayerNameRepository = Depends(get_player_name_repository),
) -> PlayerNameOffer:
    # Never golden -- see player_names.roll_golden_name. No special rate
    # limit beyond the global default: with gold off this path entirely,
    # there's nothing here worth farming, only a plain name preview.
    exclude = await _names_already_in_game(repo, join_code)
    try:
        name = await name_repo.offer_name(exclude=exclude)
    except NoAvailableNameError:
        raise HTTPException(status_code=503, detail=_NO_NAMES_AVAILABLE_DETAIL)
    return PlayerNameOffer(name=name)


@player_names_router.get("/reroll", response_model=PlayerNameOffer)
async def offer_reroll_name(
    request: Request,
    exclude: str,
    join_code: str | None = None,
    auth_user_id: str = Depends(get_auth_user_id),
    repo: GameRepository = Depends(get_repository),
    name_repo: PlayerNameRepository = Depends(get_player_name_repository),
) -> PlayerNameOffer:
    already_taken = await _names_already_in_game(repo, join_code)
    already_taken.add(exclude)
    try:
        name = await name_repo.offer_name(exclude=already_taken)
    except NoAvailableNameError:
        raise HTTPException(status_code=503, detail=_NO_NAMES_AVAILABLE_DETAIL)
    return PlayerNameOffer(name=name)
