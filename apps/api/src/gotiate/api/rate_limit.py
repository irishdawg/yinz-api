"""Shared rate limiter — module-level because slowapi's `@limiter.limit(...)`
decorators are applied at import time, not per-app-instance. `create_app()`
calls `limiter.reset()` on every call so tests using fresh app instances
don't inherit rate-limit state from earlier tests, without fighting
slowapi's decorator-based design.

Per-IP for now, via `get_remote_address` — the honest limit is per *user*,
but that requires real auth to exist first (the current stub accepts any
bearer token, so there's no trustworthy identity to key on yet). Revisit
once Supabase JWT verification replaces the stub.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])
