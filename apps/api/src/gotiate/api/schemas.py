from __future__ import annotations

from pydantic import BaseModel


class CreateGameRequest(BaseModel):
    display_name: str
    # Omitted -> GameConfig's own default (fictional_companies_v1). Validated
    # against the real ThemeRepository in routes.create_game before this ever
    # reaches the engine, so a bad id 422s immediately instead of surfacing
    # as a NotFoundError at START_GAME time.
    theme_set_id: str | None = None


class ThemeSetSummary(BaseModel):
    theme_set_id: str
    name: str


class JoinGameRequest(BaseModel):
    join_code: str
    display_name: str


class CommandRequest(BaseModel):
    command_id: str
    type: str
    expected_version: int | None = None
    payload: dict = {}


class GameSummary(BaseModel):
    game_id: str
    join_code: str
    game_player_id: str


class PlayerNameOffer(BaseModel):
    # Never golden -- the offer/reroll preview endpoints structurally
    # cannot draw gold, see player_names.roll_golden_name. Whether a seat
    # actually landed golden shows up on the roster (GamePlayer.is_golden_name
    # via project()), not here.
    name: str
