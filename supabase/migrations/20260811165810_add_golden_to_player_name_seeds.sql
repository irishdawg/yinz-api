-- Golden names: rare (1/500), only obtainable on the very first name
-- offered when opening create/join, never via reroll -- the draw logic
-- lives in PlayerNameRepository (Python), this column just marks which
-- rows are eligible to be drawn from the golden pool at all.

alter table player_name_seeds add column is_golden boolean not null default false;
