from __future__ import annotations

from fastapi import Header, HTTPException, Request

from gotiate.persistence.repository import GameRepository


async def get_repository(request: Request) -> GameRepository:
    return request.app.state.repository


async def get_auth_user_id(authorization: str | None = Header(default=None)) -> str:
    """STUB — replace with real Supabase JWT verification once Supabase auth
    is wired in. For now the bearer token *is* the user id, so the API is
    exercisable end-to-end (and by the frontend, against magic-code/dev
    tokens) before real auth exists."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="empty bearer token")
    return token
