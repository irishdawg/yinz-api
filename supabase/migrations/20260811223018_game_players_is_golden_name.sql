-- Golden-name odds moved off the free-preview offer endpoints and onto
-- actual seat creation (create_game's host, join_game's joiner) -- see
-- gotiate.domain.entities.GamePlayer.is_golden_name and player_names.py's
-- roll_golden_name. Persisted per-player (not just a one-time event) so
-- every other seated player can see who got gold, live, via the ordinary
-- roster projection -- the whole point of it being rare is that it's
-- visible, not just a private flag.

alter table game_players add column is_golden_name boolean not null default false;
