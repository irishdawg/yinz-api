from __future__ import annotations

from fastapi import Header, HTTPException, Request

from gotiate.api.auth import JWTVerificationError, verify_supabase_jwt
from gotiate.persistence.player_names import PlayerNameRepository
from gotiate.persistence.repository import GameRepository


async def get_repository(request: Request) -> GameRepository:
    return request.app.state.repository


async def get_player_name_repository(request: Request) -> PlayerNameRepository:
    return request.app.state.player_name_repository


async def get_auth_user_id(request: Request, authorization: str | None = Header(default=None)) -> str:
    """Verifies the bearer token as a real Supabase session JWT and returns
    its `sub` (stable across anonymous-to-real-account linking, so this
    never needs to change later). Every failure collapses to the same
    boring 401 — no detail on whether the token was missing, expired, or
    forged, matching the no-leakage pattern already used for rate limits."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="unauthorized")

    try:
        claims = verify_supabase_jwt(token)
    except JWTVerificationError as exc:
        raise HTTPException(status_code=401, detail="unauthorized") from exc

    auth_user_id = claims["sub"]
    request.state.auth_user_id = auth_user_id
    return auth_user_id
