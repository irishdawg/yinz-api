-- Grants -- independent of RLS, not a substitute for it (DATABASE.md).
-- authenticated/anon get SELECT only where a table is meant to be
-- directly readable, and never INSERT/UPDATE/DELETE anywhere. FastAPI's
-- gotiate_backend role gets full access to every table -- it already
-- bypasses RLS (BYPASSRLS on the role), grants are the second
-- independent layer.

revoke all on
  games, game_players, market_entities, proposals, pools,
  game_player_private, pool_contents, holdings, waterline,
  event_ledger, command_receipts
from authenticated, anon;

grant select on games, game_players, market_entities, proposals, pools
  to authenticated;

grant all on
  games, game_players, market_entities, proposals, pools,
  game_player_private, pool_contents, holdings, waterline,
  event_ledger, command_receipts
to gotiate_backend;

-- Sequences backing the uuid/int defaults on these tables also need
-- explicit grants for gotiate_backend to insert freely.
grant usage on all sequences in schema public to gotiate_backend;

-- ---------------------------------------------------------------------
-- Realtime -- Postgres Changes only streams a table once it's added
-- here. Direct-read tables only; theme_sets/theme_entities are static
-- content, nothing to subscribe to, deliberately excluded. With RLS
-- enabled, Supabase filters each subscriber's stream to what their own
-- is_game_member() check allows -- safe to broadcast broadly once added.
-- ---------------------------------------------------------------------
alter publication supabase_realtime add table games;
alter publication supabase_realtime add table game_players;
alter publication supabase_realtime add table market_entities;
alter publication supabase_realtime add table proposals;
alter publication supabase_realtime add table pools;
