-- CANCELLED is a new terminal GamePhase (see engine._handle_cancel_game) --
-- the host's escape hatch out of the one-active-game-per-host rule
-- (find_active_game_hosted_by) without waiting out real gameplay or
-- artificially forcing a scored result. Distinct from SCORED: no
-- waterline, no winner, nothing to replay.

alter table games drop constraint games_phase_check;
alter table games add constraint games_phase_check
  check (phase in ('LOBBY', 'NEGOTIATION', 'CLOSING', 'SCORED', 'CANCELLED'));
