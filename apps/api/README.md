# Gotiate API

Authoritative FastAPI backend for Gotiate ("Everything is Negotiable"). Owns all
game rules, state, and the event ledger. The frontend (Vercel/Node) and Supabase
(auth, Postgres, Realtime) never adjudicate anything themselves — see the domain
model doc for the full rules translation.

## Layout

- `src/gotiate/domain/` — the rules engine. Pure Python, zero I/O, fully unit-testable
  without a database. Everything here should be understandable by reading the domain
  model doc side by side.
- `src/gotiate/api/` — thin FastAPI layer: parses requests, resolves the caller to a
  `GamePlayer`, calls the engine, persists the result.
- `src/gotiate/persistence/` — the `GameRepository` interface. Currently an in-memory
  implementation; a Supabase/Postgres implementation will satisfy the same interface
  without the engine or API code changing.

## Local development

```bash
uv sync
uv run uvicorn gotiate.main:app --reload
uv run pytest
```

## Status

No Supabase wiring yet — auth is a stub (`api/deps.py` treats the bearer token as
the user id directly) and persistence is in-memory. Both are isolated behind
interfaces specifically so swapping them in doesn't touch `domain/`.
