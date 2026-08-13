-- Private Influence economy: a proposal/pool's Influence liability (0 or
-- 1) is now computed once, at authoring time, from the author's holdings,
-- and locked for the object's lifetime -- see the plan's design writeup.
-- Persisted here so it survives a reload/restart same as everything else
-- on Proposal/Pool.

alter table proposals add column initiator_influence_liability smallint not null default 0;
alter table pools add column initiator_influence_liability smallint not null default 0;
