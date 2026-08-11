-- Purging the original 100-name seed list. Replaced by a curated ~50-name
-- list (including the golden one) in a follow-up migration -- the table
-- is intentionally empty between this migration and that one.

delete from player_name_seeds;
