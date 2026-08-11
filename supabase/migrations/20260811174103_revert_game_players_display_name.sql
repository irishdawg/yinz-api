-- Reversing the name_seed_id normalization from two migrations ago.
-- Wrong call: player_name_seeds is reference/catalog data, not player
-- identity. Resolving display_name dynamically via join means renaming or
-- deactivating a seed name would retroactively change what an already-
-- played (or in-progress) game shows -- the exact same replay-integrity
-- problem theme_entities versioning already exists to prevent, just not
-- caught here the first time. display_name gets assigned once, at
-- create_game/join_game time, and frozen from then on -- independent of
-- whatever player_name_seeds looks like later.
--
-- Clean cut, not a backfill: confirmed zero existing rows in game_players
-- before writing this migration (same as when name_seed_id was added).

alter table game_players drop column name_seed_id;
alter table game_players add column display_name text not null;
