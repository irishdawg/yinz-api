from __future__ import annotations

from pydantic import BaseModel


class CreateGameRequest(BaseModel):
    display_name: str


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
