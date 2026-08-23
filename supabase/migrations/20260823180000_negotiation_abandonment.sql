-- Real-play gap found post-redesign: removing the gameplay clock also
-- removed the only force-close path that never depended on a player
-- doing something. A NEGOTIATION-phase game with nobody left to act on it
-- (an abandoned browser tab, a player who never returns) could previously
-- sit open forever -- and, per direct report, a returning player's own
-- auto-redirect back into their one active game then trapped them there
-- with no way out short of clearing cookies.
--
-- Adds a backstop, structurally identical to the pre-existing LOBBY
-- abandonment auto-cancel (lobby_reminder_deadline_at/
-- lobby_reminder_grace_seconds): once negotiation_abandonment_seconds
-- (config, default 600s/10min) passes with no command successfully
-- handled for the game, it force-closes via the ordinary close_market()
-- path with CloseReason.ABANDONED. Deliberately NOT a revived gameplay
-- clock -- nobody sees a countdown, nobody races it, it never affects
-- strategy.

alter table games add column last_activity_at timestamptz not null default now();

alter table games drop constraint games_close_reason_check;
alter table games add constraint games_close_reason_check
  check (close_reason in ('TIME_EXPIRED', 'READY_THRESHOLD', 'MOVES_EXHAUSTED', 'ABANDONED'));
