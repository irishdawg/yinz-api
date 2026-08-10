"""Real Supabase JWT verification, replacing the get_auth_user_id stub.

This project's live Supabase instance signs session JWTs with ES256 and
publishes the public key via its JWKS endpoint (confirmed directly against
the live project, not assumed) -- that's the only path exercised in
practice. The HS256 branch exists for portability (an older/differently
configured Supabase project) but is unused here: settings.supabase_jwt_secret
is empty, so verify_supabase_jwt always takes the JWKS path below.

Algorithm is always pinned from server-side config, never taken from the
token's own `alg` header -- letting the token pick its own algorithm is
the classic algorithm-confusion vector `jwt.decode(algorithms=[...])`
exists to prevent.
"""

from __future__ import annotations

import jwt
from jwt import PyJWKClient

from gotiate.settings import settings


class JWTVerificationError(Exception):
    pass


_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        if not settings.supabase_url:
            raise JWTVerificationError("SUPABASE_URL is not configured")
        _jwks_client = PyJWKClient(f"{settings.supabase_url}/auth/v1/.well-known/jwks.json")
    return _jwks_client


def verify_supabase_jwt(token: str) -> dict:
    """Verify a Supabase session JWT and return its claims.

    Raises JWTVerificationError on any failure (bad signature, expired,
    wrong audience/issuer, malformed token) -- callers should treat every
    failure identically (a boring 401), not branch on the specific cause.
    """
    if not settings.supabase_url:
        raise JWTVerificationError("SUPABASE_URL is not configured")
    issuer = f"{settings.supabase_url}/auth/v1"

    try:
        if settings.supabase_jwt_secret:
            # Legacy HS256 fallback -- not exercised by this project.
            return jwt.decode(
                token,
                settings.supabase_jwt_secret,
                algorithms=["HS256"],
                audience="authenticated",
                issuer=issuer,
                leeway=10,  # tolerate small clock skew between us and Supabase
            )

        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256"],
            audience="authenticated",
            issuer=issuer,
            leeway=10,  # tolerate small clock skew between us and Supabase
        )
    except jwt.PyJWTError as exc:
        raise JWTVerificationError(str(exc)) from exc
