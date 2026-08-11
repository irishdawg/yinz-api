-- Store a reference to the seed row, not a text copy -- one source of
-- truth for the name, consistent with how market_entities already stores
-- entity_id and resolves display text via theme_entities at read time
-- rather than inline. PostgresGameRepository resolves the join back into
-- GamePlayer.display_name on read; the domain model itself never changes.
--
-- Clean cut, not a backfill: confirmed zero existing rows in game_players
-- before writing this migration.

alter table game_players drop column display_name;
alter table game_players add column name_seed_id uuid not null references player_name_seeds (id);
