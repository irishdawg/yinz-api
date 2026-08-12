-- A swap exchanges two entities' positions atomically in the domain model
-- (a.position, b.position = b.position, a.position -- no invalid
-- intermediate state, ever, in memory). Persisting that as two separate
-- UPDATE statements against a plain UNIQUE(game_id, position) constraint
-- hits a transient collision: whichever entity is updated first
-- temporarily lands on a position the other entity hasn't vacated yet.
-- Never caught until Stage 3's ACCEPT_PROPOSAL path first ran against
-- real Postgres -- deferring the check to transaction commit (save()
-- already runs the whole batch inside one transaction, via lock_for())
-- fixes it generically for any reordering, not just a hand-rolled
-- two-entity special case.

alter table market_entities drop constraint market_entities_game_id_position_key;
alter table market_entities add constraint market_entities_game_id_position_key
  unique (game_id, position) deferrable initially deferred;
