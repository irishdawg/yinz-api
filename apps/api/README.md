# Gotiate API

Authoritative FastAPI backend for Gotiate ("Everything is Negotiable"). Owns all
game rules, state, and the event ledger. The frontend (Vercel/Node) and Supabase
(auth, Postgres, Realtime) never adjudicate anything themselves — see
`../../GAMEPLAY.md` at the repo root for the current rules, and this repo's
root `AGENTS.md` for the full architecture map.

## Layout

- `src/gotiate/domain/` — the rules engine. Pure Python, zero I/O, fully unit-testable
  without a database. Read alongside `GAMEPLAY.md`.
- `src/gotiate/api/` — thin FastAPI layer: parses requests, resolves the caller to a
  `GamePlayer`, calls the engine, persists the result. Auth (`auth.py`, real Supabase
  JWT/JWKS verification) and the gateway-secret middleware (`main.py`) also live here.
- `src/gotiate/persistence/` — the `GameRepository` Protocol. `repository.py` also
  holds the in-memory implementation every test runs against;
  `postgres_repository.py` is the real implementation `main.py` serves in production.

## Local development

```bash
uv sync
uv run uvicorn gotiate.main:app --reload    # ALWAYS with --reload on Windows — see root AGENTS.md
uv run pytest
```

## Status

Fully wired: real Supabase JWT verification, `PostgresGameRepository` in
production (in-memory only in tests), the gateway-secret middleware, and every
gameplay mechanic through Market Correction (see `../../GAMEPLAY.md`) are
live. For unresolved decisions and known gaps, see `../../CURRENT_WORK.md`.
