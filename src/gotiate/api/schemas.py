from __future__ import annotations

from pydantic import BaseModel, Field


class CreateGameRequest(BaseModel):
    display_name: str
    # Soft hint, not a cap — see Game.expected_player_count. Optional; if the
    # host doesn't specify one, they just trigger START_GAME whenever ready.
    expected_player_count: int | None = Field(default=None, ge=2, le=6)


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
