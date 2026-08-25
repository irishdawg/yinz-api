-- New feature: KICK_PLAYER (host-only, LOBBY-only) -- lets the host remove
-- a player outright (e.g. an offensive typed display name; typed names
-- have no content-moderation filter by design, see CURRENT_WORK.md), fully
-- deleting their game_players/game_player_private rows so the same
-- auth_user_id is immediately free to rejoin under a different name
-- (game_player_private's own `unique (game_id, auth_user_id)` would
-- otherwise block exactly that if the old row just stuck around soft
-- -deleted).
--
-- A hard delete of game_players surfaced a real, previously-latent
-- constraint problem: event_ledger.actor_game_player_id and
-- command_receipts.actor_game_player_id both referenced game_players with
-- the default NO ACTION delete rule -- and every player has at least one
-- event_ledger row (their own PLAYER_JOINED, actor = their own id), so
-- deleting their game_players row would have failed the FK outright.
-- Fixed by widening both to SET NULL. Losing actor attribution on a
-- kicked player's own historical events is an acceptable (arguably
-- correct) tradeoff for a moderation feature whose whole point is
-- removing someone's identity from the game going forward -- GameEvent
-- .actor_game_player_id is already nullable in the domain model for
-- exactly this kind of system-attributed-or-unattributed case.
--
-- No other FK referencing game_players needs touching: KICK_PLAYER is
-- LOBBY-only, and holdings/proposals/pools/proposal_passes can only ever
-- reference a player who reached NEGOTIATION -- structurally impossible
-- for someone who got kicked before START_GAME. games.host_player_id is
-- also unaffected -- the host can never kick themselves.

alter table event_ledger drop constraint event_ledger_actor_game_player_id_fkey;
alter table event_ledger add constraint event_ledger_actor_game_player_id_fkey
  foreign key (actor_game_player_id) references game_players (id) on delete set null;

alter table command_receipts drop constraint command_receipts_actor_game_player_id_fkey;
alter table command_receipts add constraint command_receipts_actor_game_player_id_fkey
  foreign key (actor_game_player_id) references game_players (id) on delete set null;
