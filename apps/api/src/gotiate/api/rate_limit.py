"""Shared rate limiter — module-level because slowapi's `@limiter.limit(...)`
decorators are applied at import time, not per-app-instance. `create_app()`
calls `limiter.reset()` on every call so tests using fresh app instances
don't inherit rate-limit state from earlier tests, without fighting
slowapi's decorator-based design.

Keyed on the real originating client IP when the gateway supplies one,
falling back to the verified auth_user_id, then to the direct remote
address. Not just per-user: for create_game specifically, one-active-
game-per-host already caps a single verified identity to one concurrent
game, so the 5/hour limit's actual job is stopping one real actor from
spinning up many *different* anonymous identities in a burst — a purely
per-user key can't see that at all, since each new anonymous sign-in is a
fresh, never-before-seen auth_user_id with its own empty bucket.

`X-Gotiate-Client-IP` is set by the Next.js gateway from the browser's
real IP (as Vercel's edge sees it) on every request it forwards to
FastAPI. It's trustworthy specifically *because* GatewaySecretMiddleware
(main.py) has already rejected anything that isn't a legitimate gateway
call before this ever runs -- an attacker who doesn't have the gateway
secret never reaches this key_func at all, so there's no incentive to
spoof the header even though nothing here cryptographically ties it to
the request. A custom header, not the standard X-Forwarded-For, because
Render's own edge proxy sets X-Forwarded-For to whatever peer connected to
it (Vercel's function IP once traffic is proxied) and there's no need to
untangle two different actors' conventions for the same header.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request


def _rate_limit_key(request: Request) -> str:
    forwarded_ip = request.headers.get("x-gotiate-client-ip")
    if forwarded_ip:
        return forwarded_ip
    auth_user_id = getattr(request.state, "auth_user_id", None)
    return auth_user_id or get_remote_address(request)


limiter = Limiter(key_func=_rate_limit_key, default_limits=["200/minute"])
