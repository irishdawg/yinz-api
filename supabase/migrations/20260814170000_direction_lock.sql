-- Market-direction-reversal locking: a proposal/pool's "rising" entity is
-- now locked once, at authoring time, instead of derived live from
-- current market positions on every read -- see the design writeup.
-- Existing OPEN rows predate this column and never had a chance to lock
-- anything, so they're backfilled here from their CURRENT market
-- position at migration time (same "don't leave old rows on a different,
-- unguarded code path" reasoning as the Haircut-economy incident this
-- session), then the column is made not null. Already-RESOLVED rows are
-- backfilled the same way purely to satisfy not null -- their direction
-- is never read again once resolved.

alter table proposals add column rising_entity_id text;

update proposals p set rising_entity_id = (
  select case when me_a.position > me_b.position then p.entity_a else p.entity_b end
  from market_entities me_a, market_entities me_b
  where me_a.game_id = p.game_id and me_a.entity_id = p.entity_a
    and me_b.game_id = p.game_id and me_b.entity_id = p.entity_b
);

alter table proposals alter column rising_entity_id set not null;
alter table proposals add constraint proposals_rising_entity_id_check
  check (rising_entity_id = entity_a or rising_entity_id = entity_b);
alter table proposals add foreign key (game_id, rising_entity_id) references market_entities (game_id, entity_id);

alter table proposals drop constraint proposals_resolution_reason_check;
alter table proposals add constraint proposals_resolution_reason_check
  check (resolution_reason in (
    'executed', 'withdrawn_by_initiator', 'market_closed', 'expired_all_passed', 'voided_market_swung'
  ));

-- pool_contents, not pools -- direction is content, gated the same as
-- entity_c/entity_d (see projections._project_pool: hidden for a still
-- -private pool, revealed together with the rest of its contents).
alter table pool_contents add column rising_entity_id text;

update pool_contents pc set rising_entity_id = (
  select case when me_c.position > me_d.position then pc.entity_c else pc.entity_d end
  from market_entities me_c, market_entities me_d
  where me_c.game_id = pc.game_id and me_c.entity_id = pc.entity_c
    and me_d.game_id = pc.game_id and me_d.entity_id = pc.entity_d
);

alter table pool_contents alter column rising_entity_id set not null;
alter table pool_contents add constraint pool_contents_rising_entity_id_check
  check (rising_entity_id = entity_c or rising_entity_id = entity_d);
alter table pool_contents add foreign key (game_id, rising_entity_id) references market_entities (game_id, entity_id);

alter table pools drop constraint pools_resolution_reason_check;
alter table pools add constraint pools_resolution_reason_check
  check (resolution_reason in (
    'executed', 'withdrawn_by_initiator', 'invalidated_by_initiator_action',
    'declined_by_target', 'preempted_by_other_action', 'base_proposal_withdrawn',
    'market_closed', 'voided_market_swung', 'base_proposal_voided'
  ));
