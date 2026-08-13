"""Theme sets are swappable config, not hardcoded content — see themes.py
and the domain model's ThemeSet/ThemeEntityDefinition split.

Two different failure classes matter here, and they're tested differently:
tests *about the swap mechanism itself* (too-small, too-many-locked, unknown
set) use a fake ThemeRepository with a deliberately tiny fixture, so they
never depend on how large real content happens to be today. Tests *about
real content* (dragons_v1, cats_v1, fictional_companies_v1) read expectations
from the theme set itself rather than hardcoding names/counts, so editing
content in the IDE doesn't silently break coverage of something else.
"""

from __future__ import annotations

import pytest

from gotiate.domain import engine, themes
from gotiate.domain.entities import GameConfig, HaircutProfile
from gotiate.domain.errors import IllegalCommandError, NotFoundError
from gotiate.domain.projections import PlayerAudience, project
from gotiate.domain.themes import ThemeEntityDefinition, ThemeRepository, ThemeSet
from tests.conftest import now

REAL_THEME_SETS = ["fictional_companies_v1", "dragons_v1", "cats_v1"]


def _start_with_theme(theme_set_id: str, player_count: int):
    game, _ = engine.create_game(
        actor_auth_user_id="auth-0", display_name="Host", now=now(), config=GameConfig(theme_set_id=theme_set_id)
    )
    for i in range(1, player_count):
        engine.join_game(game, actor_auth_user_id=f"auth-{i}", display_name=f"Player {i}", now=now())
    events = engine.handle_command(
        game, command_type="START_GAME", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=now()
    )
    return game, events


class _FixedThemeRepository:
    """A one-set fake, so tests about the swap mechanism itself don't depend
    on any real content file's size or contents."""

    def __init__(self, theme_set: ThemeSet) -> None:
        self._theme_set = theme_set

    def get(self, theme_set_id: str) -> ThemeSet:
        if theme_set_id != self._theme_set.theme_set_id:
            raise NotFoundError(theme_set_id)
        return self._theme_set

    def list_ids(self) -> list[str]:
        return [self._theme_set.theme_set_id]


def _tiny_theme_set(theme_set_id: str = "tiny_v1", n: int = 5, locked: int = 0) -> ThemeSet:
    entities = [
        ThemeEntityDefinition(theme_key=f"t{i}", display_name=f"Thing {i}", ticker_symbol=f"T{i}", is_locked=i < locked)
        for i in range(n)
    ]
    return ThemeSet(theme_set_id=theme_set_id, name="Tiny", version=1, entities=entities)


@pytest.fixture
def fake_theme_repository():
    """Swaps in a fresh fake repository for one test, restoring the real one
    afterward so nothing leaks into other tests."""
    original: ThemeRepository = themes.get_theme_repository()
    fake = _FixedThemeRepository(_tiny_theme_set())
    themes.set_theme_repository(fake)
    yield fake
    themes.set_theme_repository(original)


def test_default_theme_set_is_the_free_companies_set():
    game, _ = engine.create_game(actor_auth_user_id="auth-0", display_name="Host", now=now())
    assert game.config.theme_set_id == "fictional_companies_v1"


@pytest.mark.parametrize("theme_set_id", REAL_THEME_SETS)
def test_swapping_theme_set_only_uses_that_sets_content(theme_set_id):
    game, _ = _start_with_theme(theme_set_id, player_count=2)
    view = project(game, PlayerAudience(game.host_player_id))
    dealt_names = {m["display_name"] for m in view["market"]}

    this_set_names = {e.display_name for e in themes.get_theme_set(theme_set_id).entities}
    other_names = {
        e.display_name for other_id in REAL_THEME_SETS if other_id != theme_set_id for e in themes.get_theme_set(other_id).entities
    }

    assert dealt_names <= this_set_names
    assert dealt_names.isdisjoint(other_names)


@pytest.mark.parametrize("theme_set_id", REAL_THEME_SETS)
@pytest.mark.parametrize("player_count", [2, 3, 4, 5, 6])
def test_start_game_succeeds_for_every_real_theme_set_and_player_count(theme_set_id, player_count):
    game, _ = _start_with_theme(theme_set_id, player_count)
    assert game.phase.value == "NEGOTIATION"
    assert len(game.market) == game.config.market_size_by_players[player_count]


@pytest.mark.parametrize("theme_set_id", REAL_THEME_SETS)
def test_locked_entities_are_always_dealt_into_the_market(theme_set_id):
    # 2 players is the smallest market (9) against the largest real content
    # (40) — the most sampling pressure available, not the trivial case
    # where everything fits regardless.
    locked_keys = {e.theme_key for e in themes.get_theme_set(theme_set_id).entities if e.is_locked}
    assert locked_keys, f"{theme_set_id} fixture has no locked entities to actually test against"

    game, _ = _start_with_theme(theme_set_id, player_count=2)
    theme_keys = {m["theme_key"] for m in project(game, PlayerAudience(game.host_player_id))["market"]}
    assert locked_keys <= theme_keys


@pytest.mark.parametrize("theme_set_id", REAL_THEME_SETS)
def test_market_entities_carry_a_ticker_symbol_and_logo_slot(theme_set_id):
    game, _ = _start_with_theme(theme_set_id, player_count=2)
    view = project(game, PlayerAudience(game.host_player_id))
    for entity in view["market"]:
        assert entity["ticker_symbol"]
        assert len(entity["ticker_symbol"]) <= 6
        assert "logo_url" in entity  # present even when None — no logos yet, but the slot exists


def test_theme_set_too_small_for_player_count_is_rejected_cleanly(fake_theme_repository):
    game, _ = engine.create_game(actor_auth_user_id="auth-0", display_name="Host", now=now(), config=GameConfig(theme_set_id="tiny_v1"))
    for i in range(1, 4):  # 4 players needs 13 entities; the fake set has 5
        engine.join_game(game, actor_auth_user_id=f"auth-{i}", display_name=f"P{i}", now=now())

    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game, command_type="START_GAME", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=now()
        )


def test_too_many_locked_entities_for_market_size_is_rejected_cleanly(fake_theme_repository):
    themes.set_theme_repository(_FixedThemeRepository(_tiny_theme_set(n=5, locked=5)))
    game, _ = engine.create_game(
        actor_auth_user_id="auth-0",
        display_name="Host",
        now=now(),
        config=GameConfig(
            theme_set_id="tiny_v1",
            market_size_by_players={2: 3},
            # Overriding market_size_by_players alone would leave the default
            # haircut_profiles_by_players[2] (authored for market_size=9)
            # mismatched against the new size=3 market -- GameConfig's own
            # validator catches that drift, so this override has to travel
            # with a matching profile, unrelated as it is to what this test
            # actually exercises (locked-entity count vs. market size).
            haircut_profiles_by_players={2: [HaircutProfile(depth_probabilities=[0.7, 0.3])]},
        ),
    )
    engine.join_game(game, actor_auth_user_id="auth-1", display_name="P1", now=now())

    with pytest.raises(IllegalCommandError):
        engine.handle_command(
            game, command_type="START_GAME", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=now()
        )


def test_unknown_theme_set_is_rejected_cleanly():
    game, _ = engine.create_game(
        actor_auth_user_id="auth-0", display_name="Host", now=now(), config=GameConfig(theme_set_id="does_not_exist_v1")
    )
    engine.join_game(game, actor_auth_user_id="auth-1", display_name="P1", now=now())

    with pytest.raises(NotFoundError):
        engine.handle_command(
            game, command_type="START_GAME", payload={}, actor_game_player_id=game.host_player_id, expected_version=None, now=now()
        )
