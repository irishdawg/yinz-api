-- is_active lets a name be retired from future offers without deleting
-- the row (which would break the unique constraint's guarantee for
-- anything that ever referenced it by value) or breaking historical
-- meaning. Distinct from is_golden and from usage_count: a name can be
-- active-and-common, active-and-golden, or inactive (any prior
-- usage_count is preserved either way).
--
-- Deliberately NOT consulted by is_valid_name() (PostgresPlayerNameRepository)
-- -- that check is existence-only, so a name offered while active but
-- retired moments before submission doesn't suddenly become rejectable.
-- Only the offer methods (offer_initial_name/offer_reroll_name) filter on
-- is_active = true, since that's what actually controls what gets shown
-- going forward.

alter table player_name_seeds add column is_active boolean not null default true;
alter table player_name_seeds add constraint player_name_seeds_usage_count_check check (usage_count >= 0);
