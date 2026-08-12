-- expected_player_count was purely decorative -- never capped joins (the
-- hard 6-seat limit already does that) and never triggered a start (only
-- the host's Start button does). Removed entirely rather than left as
-- dead weight; the lobby always allows up to 6.
--
-- Replaced by a lobby-only reminder/grace/auto-cancel cycle: the only way
-- LOBBY -> NEGOTIATION happens is still the host hitting Start, but an
-- abandoned lobby no longer sits open forever. See
-- gotiate.domain.engine.apply_due_time_transitions and
-- _handle_extend_lobby_timer.

alter table games drop column expected_player_count;
alter table games add column lobby_reminder_deadline_at timestamptz;
alter table games add column cancellation_reason text
  check (cancellation_reason in ('HOST_INITIATED', 'LOBBY_TIMEOUT'));
