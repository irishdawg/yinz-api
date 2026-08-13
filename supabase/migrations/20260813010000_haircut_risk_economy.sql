-- Retires the Waterline scoring model entirely in favour of a public
-- Haircut-risk economy -- see the design writeup. The hidden Waterline
-- entity is gone (qualifying-count + lexicographic tiebreak is gone with
-- it); scoring is now a value-sum over surviving (non-wiped) holdings,
-- where the wipe depth is a single correlated draw from a per-game
-- HaircutProfile, revealed publicly at the game's halfway point.
--
-- The profile itself (and the realized depth once scored) is stored
-- directly on games, not in a dedicated table like waterline was --
-- there's no foreign key to market_entities to enforce here, it's just
-- data (a probability distribution) plus a couple of timestamps.

alter table games drop column waterline_revealed;
drop table waterline;

alter table games add column haircut_profile jsonb;
alter table games add column haircut_reveal_at timestamptz;
alter table games add column haircut_profile_revealed_at timestamptz;
alter table games add column realized_haircut_depth smallint;
