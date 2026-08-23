-- Cadence/economy redesign (prototype branch), checkpoint 6: schema
-- cleanup. Drops every column that checkpoints 1-4 stopped reading/writing
-- (Influence, Market Correction, the gameplay clock/unilateral window, the
-- old Reserve/Pickup mechanic, the 5-6 player multi-accept threshold), and
-- widens (never narrows) the CHECK constraints those checkpoints left
-- stale so a genuinely legal current value is always accepted.
-- Additive-only migrations are never edited after the fact (see
-- AGENTS.md) -- this is the deliberate forward drop those checkpoints'
-- own comments promised.
--
-- Constraints are widened, not tightened, on purpose: every pre-redesign
-- SCORED game still live in this project actually used the old values
-- (TIME_EXPIRED, withdrawn_by_initiator on a base proposal,
-- base_proposal_withdrawn, and every old reserve zone) -- rewriting that
-- historical data to a reason that isn't true of it would be worse than
-- leaving an unreachable-going-forward value legal in the CHECK. No new
-- code path can ever write these again (ProposalResolutionReason/
-- PoolResolutionReason/HoldingZone/CloseReason no longer have them as
-- members at the domain layer) -- this is purely about not breaking
-- historical rows already on disk.
--
-- Also fixes a real live bug caught by this pass, not just tidiness:
-- games_close_reason_check still only allowed ('TIME_EXPIRED',
-- 'READY_THRESHOLD') -- checkpoint 2 introduced CloseReason.MOVES_EXHAUSTED
-- but never updated this constraint, so the first real game to close via
-- Move exhaustion would have failed this CHECK at the database level. No
-- test caught it because InMemoryGameRepository enforces no such
-- constraint -- exactly the class of gap the "verify against real
-- Postgres" discipline this whole redesign has followed exists to catch.

-- --- games ---------------------------------------------------------
alter table games drop column max_duration_s;
alter table games drop column unilateral_cutoff_at;
alter table games drop column unilateral_window_closed_at;
alter table games drop column haircut_reveal_at; -- superseded by haircut_profile_revealed_at (Move-driven reveal, not clock-driven)
alter table games drop column pending_market_correction;
alter table games drop column last_negotiated_execution_at;
alter table games drop column market_correction_cooldown_until;

alter table games drop constraint games_close_reason_check;
alter table games add constraint games_close_reason_check
  check (close_reason in ('TIME_EXPIRED', 'READY_THRESHOLD', 'MOVES_EXHAUSTED'));

-- --- game_players ----------------------------------------------------
alter table game_players drop column influence_available;
alter table game_players drop column influence_committed;
alter table game_players drop column influence_spent;
alter table game_players drop column reserve_count_remaining;

-- --- game_player_private ----------------------------------------------
-- Superseded by pending_boost_draw (checkpoint 4) -- same FastAPI-only
-- posture, same frozen-view pattern, new column rather than a reused one
-- per "never edit an applied migration."
alter table game_player_private drop column pending_pickup;

-- --- proposals ---------------------------------------------------------
alter table proposals drop column initiator_influence_liability;
alter table proposals drop column pending_accepters;

alter table proposals drop constraint proposals_resolution_reason_check;
alter table proposals add constraint proposals_resolution_reason_check
  check (resolution_reason in (
    'executed', 'market_closed', 'expired_all_passed', 'voided_market_swung', 'arbitration_neither',
    'withdrawn_by_initiator' -- historical only; a base proposal can no longer be withdrawn (rule 4)
  ));

-- --- pools ---------------------------------------------------------
alter table pools drop column initiator_influence_liability;
alter table pools drop column pending_accepters;

alter table pools drop constraint pools_resolution_reason_check;
alter table pools add constraint pools_resolution_reason_check
  check (resolution_reason in (
    'executed', 'withdrawn_by_initiator', 'invalidated_by_initiator_action',
    'declined_by_target', 'preempted_by_other_action', 'market_closed',
    'voided_market_swung', 'base_proposal_voided',
    'base_proposal_withdrawn' -- historical only; superseded by base_proposal_voided
  ));

-- --- holdings ---------------------------------------------------------
alter table holdings drop constraint holdings_zone_check;
alter table holdings add constraint holdings_zone_check
  check (zone in (
    'portfolio', 'discarded',
    -- Historical only -- every unrevealed-to-owner reserve zone from the
    -- old Reserve/Pickup mechanic. No code path has produced any of these
    -- since checkpoint 1.
    'reserve_unrevealed', 'pickup_pending', 'pickup_surrendered', 'surrendered_unused', 'burned_unseen'
  ));
