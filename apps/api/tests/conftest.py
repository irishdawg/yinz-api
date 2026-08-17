from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gotiate.domain import engine
from gotiate.domain.entities import Game, GameConfig, HoldingZone


def now() -> datetime:
    return datetime.now(timezone.utc)


def later(seconds: float = 5) -> datetime:
    """now() pushed past GameConfig.accept_lock_seconds's default (4s) --
    pass as `now` to an ACCEPT_PROPOSAL/ACCEPT_POOL call in a test that
    proposes and accepts back-to-back, so the accept-lock grace period
    (engine._require_accept_unlocked) doesn't reject it."""
    return now() + timedelta(seconds=seconds)


def make_started_game(player_count: int = 4, *, config: GameConfig | None = None) -> Game:
    game, _ = engine.create_game(actor_auth_user_id="auth-0", display_name="Host", now=now(), config=config)
    for i in range(1, player_count):
        engine.join_game(game, actor_auth_user_id=f"auth-{i}", display_name=f"Player {i}", now=now())
    engine.handle_command(
        game,
        command_type="START_GAME",
        payload={},
        actor_game_player_id=game.host_player_id,
        expected_version=None,
        now=now(),
    )
    return game


def _owns(game: Game, player_id: str, entity_id: str) -> bool:
    return any(h.owner_player_id == player_id and h.zone == HoldingZone.PORTFOLIO and h.entity_id == entity_id for h in game.holdings.values())


def find_swap_pair(
    game: Game,
    player_id: str,
    *,
    owned_should_rise: bool,
    exclude: frozenset[str] = frozenset(),
    not_owned_by: str | None = None,
) -> tuple[str, str]:
    """A (entity_a, entity_b) pair for player_id -- one entity they own,
    one they don't. *Constructs* the desired relative order rather than
    searching the random deal for it: picks one owned and one unowned
    entity, then swaps their two market positions (if needed) so the
    owned one is/isn't the rising side, per engine._rising_entity's
    "higher position = rises" rule. A pure search over a small (5-entity)
    owned set against a small unowned pool can genuinely have no match at
    all depending on how the random deal fell -- entities are dealt with
    repetition, so ownership across players can overlap, and a strict
    direction can be unsatisfiable by chance; constructing it is
    deterministic regardless. `exclude` keeps a pair from reusing entities
    already claimed by another proposal/pool in the same test (the engine
    forbids referencing a base proposal's own entities in a pool on it).
    `not_owned_by`, when given, prefers a candidate for whichever side
    ends up rising that the second player also doesn't own -- for
    isolating one player's liability from a second player's unrelated
    fresh accept-time charge on the same swap."""
    owned = {h.entity_id for h in game.holdings.values() if h.owner_player_id == player_id and h.zone == HoldingZone.PORTFOLIO} - exclude
    unowned = set(game.market) - owned - exclude
    assert owned, f"{player_id} owns nothing eligible"
    assert unowned, "no unowned entities left to pair with"

    def eligible_as_rising(eid: str) -> bool:
        return not_owned_by is None or not _owns(game, not_owned_by, eid)

    if owned_should_rise:
        oe = next((e for e in owned if eligible_as_rising(e)), next(iter(owned)))
        ue = next(iter(unowned - {oe}))
    else:
        ue = next((e for e in unowned if eligible_as_rising(e)), next(iter(unowned)))
        oe = next(iter(owned - {ue}))

    currently_rises = game.market[oe].position > game.market[ue].position
    if currently_rises != owned_should_rise:
        game.market[oe].position, game.market[ue].position = game.market[ue].position, game.market[oe].position
    return oe, ue
