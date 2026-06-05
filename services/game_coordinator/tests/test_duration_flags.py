"""
Tests for #766 F2: durations, roles, and scoring promoted to game flags.

Covers, per the issue's consistency rules:
- Characterization: every game.json default reproduces the current hardcoded
  behavior exactly (durations, role counts at various player counts, scoring).
- Malformed/out-of-range flag values fall back to the module constants
  (the source of truth) per flag.
- Init-frozen: each mode reads its flags once at __init__; a mid-game flag
  change has no effect on the already-resolved instance values.
"""

import sys
from pathlib import Path
from unittest.mock import patch

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, async_noop

from services.game_coordinator.games.base import (
    DEATH_GRACE_PERIOD,
    resolve_non_negative_duration,
)
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.fight_club import ROUND_DURATION, FightClubGame
from services.game_coordinator.games.nonstop_joust import (
    RESPAWN_DURATION,
    SCORE_BASE,
    SCORE_DEATH_PENALTY,
    SPAWN_PROTECTION_DURATION,
    NonstopJoustGame,
    resolve_scoring,
)
from services.game_coordinator.games.tournament import (
    MATCH_DURATION,
    TIME_BETWEEN_MATCHES,
    TournamentGame,
)
from services.game_coordinator.games.traitor import (
    DEFAULT_TRAITOR_COUNT_TIERS,
    DEFAULT_TRAITOR_FALLBACK_DIVISOR,
    TraitorGame,
    resolve_count_tiers,
)
from services.game_coordinator.games.werewolf import (
    WEREWOLF_FRACTION,
    WerewolfGame,
    resolve_fraction,
)
from services.game_coordinator.games.zombie import (
    INITIAL_ZOMBIES,
    ZOMBIE_RESPAWN_MAX,
    ZOMBIE_RESPAWN_MIN,
    ZombieGame,
)

# ========================================================================
# Helpers
# ========================================================================


def _make_ffa():
    return FFAGame(
        controller_manager_client=None,
        event_publisher=EventCollector().publish,
        sensitivity=2,
    )


def _make_nonstop():
    return NonstopJoustGame(
        controller_manager_client=None,
        event_publisher=async_noop,
        sensitivity=2,
    )


def _make_fight_club():
    return FightClubGame(
        controller_manager_client=None,
        event_publisher=async_noop,
        sensitivity=2,
    )


def _make_tournament():
    return TournamentGame(
        controller_manager_client=None,
        event_publisher=async_noop,
        sensitivity=2,
    )


def _make_werewolf():
    return WerewolfGame(
        controller_manager_client=None,
        event_publisher=async_noop,
        sensitivity=2,
    )


def _make_traitor():
    return TraitorGame(
        controller_manager_client=None,
        event_publisher=async_noop,
        sensitivity=2,
    )


def _make_zombie():
    return ZombieGame(
        controller_manager_client=None,
        event_publisher=async_noop,
        sensitivity=2,
    )


# ========================================================================
# resolve_non_negative_duration
# ========================================================================


def test_duration_valid_positive_and_zero():
    assert resolve_non_negative_duration(3.0, 1.0) == 3.0
    assert resolve_non_negative_duration(0, 1.0) == 0.0
    assert resolve_non_negative_duration(7, 1.0) == 7.0


def test_duration_rejects_negative_nonnumeric_bool_nan_inf():
    assert resolve_non_negative_duration(-1.0, 1.0) == 1.0
    assert resolve_non_negative_duration("x", 1.0) == 1.0
    assert resolve_non_negative_duration(None, 1.0) == 1.0
    assert resolve_non_negative_duration(True, 1.0) == 1.0
    assert resolve_non_negative_duration(float("nan"), 1.0) == 1.0
    assert resolve_non_negative_duration(float("inf"), 1.0) == 1.0


# ========================================================================
# death_grace_period (base.py)
# ========================================================================


def test_grace_default_reproduces_constant():
    game = _make_ffa()
    assert game.death_grace_period == DEATH_GRACE_PERIOD


def test_grace_reads_flag_once_and_frozen():
    with patch("services.game_coordinator.games.base.read_float_flag", return_value=0.25) as mock_read:
        game = _make_ffa()
    assert game.death_grace_period == 0.25
    # exactly one read for the grace flag (init-frozen)
    grace_calls = [c for c in mock_read.call_args_list if c.args[1] == "death_grace_period_seconds"]
    assert len(grace_calls) == 1


def test_grace_malformed_falls_back():
    with patch("services.game_coordinator.games.base.read_float_flag", return_value=-5.0):
        game = _make_ffa()
    assert game.death_grace_period == DEATH_GRACE_PERIOD


# ========================================================================
# Nonstop: respawn / spawn-protection / scoring
# ========================================================================


def test_nonstop_defaults_reproduce_constants():
    game = _make_nonstop()
    assert game.respawn_duration == RESPAWN_DURATION
    assert game.spawn_protection_duration == SPAWN_PROTECTION_DURATION
    assert game.score_base == SCORE_BASE
    assert game.score_death_penalty == SCORE_DEATH_PENALTY


def test_nonstop_scoring_resolver_characterization():
    # The game.json default object must reproduce the 100 - deaths*10 formula.
    base, penalty = resolve_scoring({"base": 100, "death_penalty": 10}, SCORE_BASE, SCORE_DEATH_PENALTY)
    assert base == 100
    assert penalty == 10
    for deaths in (0, 1, 3, 11, 20):
        assert max(0, base - deaths * penalty) == max(0, 100 - deaths * 10)


def test_nonstop_scoring_resolver_fallbacks():
    # non-dict, negative, non-int, bool, missing keys -> default
    assert resolve_scoring(None, 100, 10) == (100, 10)
    assert resolve_scoring({"base": -5, "death_penalty": 10}, 100, 10) == (100, 10)
    assert resolve_scoring({"base": 2.5, "death_penalty": 10}, 100, 10) == (100, 10)
    assert resolve_scoring({"base": True, "death_penalty": 10}, 100, 10) == (100, 10)
    assert resolve_scoring({"base": 50}, 100, 10) == (50, 10)  # missing penalty -> default


def test_nonstop_reads_frozen_override():
    with (
        patch(
            "services.game_coordinator.games.nonstop_joust.read_float_flag",
            side_effect=[1.5, 4.0],  # respawn, spawn_protection
        ),
        patch(
            "services.game_coordinator.games.nonstop_joust.read_object_flag",
            return_value={"base": 200, "death_penalty": 25},
        ),
    ):
        game = _make_nonstop()
    assert game.respawn_duration == 1.5
    assert game.spawn_protection_duration == 4.0
    assert game.score_base == 200
    assert game.score_death_penalty == 25


def test_nonstop_malformed_durations_fall_back():
    with (
        patch(
            "services.game_coordinator.games.nonstop_joust.read_float_flag",
            side_effect=[-1.0, "bad"],
        ),
        patch(
            "services.game_coordinator.games.nonstop_joust.read_object_flag",
            return_value="not-a-dict",
        ),
    ):
        game = _make_nonstop()
    assert game.respawn_duration == RESPAWN_DURATION
    assert game.spawn_protection_duration == SPAWN_PROTECTION_DURATION
    assert game.score_base == SCORE_BASE
    assert game.score_death_penalty == SCORE_DEATH_PENALTY


# ========================================================================
# Fight Club: round duration
# ========================================================================


def test_fight_club_default_round_duration():
    game = _make_fight_club()
    assert game._round_duration == ROUND_DURATION


def test_fight_club_round_override_and_zero_fallback():
    with patch("services.game_coordinator.games.fight_club.read_float_flag", return_value=30.0):
        game = _make_fight_club()
    assert game._round_duration == 30.0
    # A 0s round would break play -> fall back to the constant.
    with patch("services.game_coordinator.games.fight_club.read_float_flag", return_value=0.0):
        game = _make_fight_club()
    assert game._round_duration == ROUND_DURATION


# ========================================================================
# Tournament: match duration + inter-match pause
# ========================================================================


def test_tournament_defaults():
    game = _make_tournament()
    assert game._match_duration == MATCH_DURATION
    assert game._time_between_matches == TIME_BETWEEN_MATCHES


def test_tournament_overrides_and_fallbacks():
    with patch(
        "services.game_coordinator.games.tournament.read_float_flag",
        side_effect=[18.0, 0.0],  # match=18s, pause=0s (0 pause allowed)
    ):
        game = _make_tournament()
    assert game._match_duration == 18.0
    assert game._time_between_matches == 0.0
    # 0s match -> fall back; bad pause -> fall back
    with patch(
        "services.game_coordinator.games.tournament.read_float_flag",
        side_effect=[0.0, "bad"],
    ):
        game = _make_tournament()
    assert game._match_duration == MATCH_DURATION
    assert game._time_between_matches == TIME_BETWEEN_MATCHES


# ========================================================================
# Werewolf: werewolf fraction
# ========================================================================


def test_fraction_resolver():
    assert resolve_fraction(0.44, 0.44) == 0.44
    assert resolve_fraction(0.5, 0.44) == 0.5
    # boundaries (0 and 1) and out-of-range -> default
    for bad in (0.0, 1.0, -0.1, 1.5, "x", None, True, float("nan")):
        assert resolve_fraction(bad, 0.44) == 0.44


def test_werewolf_fraction_default_characterization():
    """Default 0.44 reproduces the original werewolf-count math at several sizes."""
    game = _make_werewolf()
    assert game.werewolf_fraction == WEREWOLF_FRACTION
    for num_players in (2, 4, 5, 8, 13, 20):
        expected = max(1, int(num_players * 0.44))
        assert max(1, int(num_players * game.werewolf_fraction)) == expected


def test_werewolf_fraction_override_and_fallback():
    with patch("services.game_coordinator.games.werewolf.read_float_flag", return_value=0.5):
        game = _make_werewolf()
    assert game.werewolf_fraction == 0.5
    with patch("services.game_coordinator.games.werewolf.read_float_flag", return_value=1.5):
        game = _make_werewolf()
    assert game.werewolf_fraction == WEREWOLF_FRACTION


# ========================================================================
# Traitor: count tiers
# ========================================================================


def test_count_tiers_resolver_default():
    tiers, divisor = resolve_count_tiers(
        {
            "tiers": [
                {"max_players": 5, "count": 1},
                {"max_players": 8, "count": 2},
                {"max_players": 11, "count": 3},
            ],
            "fallback_divisor": 3,
        },
        DEFAULT_TRAITOR_COUNT_TIERS,
        DEFAULT_TRAITOR_FALLBACK_DIVISOR,
    )
    assert tiers == DEFAULT_TRAITOR_COUNT_TIERS
    assert divisor == 3


def test_count_tiers_resolver_fallbacks():
    d = (DEFAULT_TRAITOR_COUNT_TIERS, DEFAULT_TRAITOR_FALLBACK_DIVISOR)
    assert resolve_count_tiers(None, *d) == d
    assert resolve_count_tiers({"tiers": [], "fallback_divisor": 3}, *d) == d
    assert resolve_count_tiers({"tiers": [{"max_players": 5, "count": 1}], "fallback_divisor": 0}, *d) == d
    # non-ascending max_players
    bad_order = {
        "tiers": [{"max_players": 8, "count": 1}, {"max_players": 5, "count": 2}],
        "fallback_divisor": 3,
    }
    assert resolve_count_tiers(bad_order, *d) == d
    # count < 1
    bad_count = {"tiers": [{"max_players": 5, "count": 0}], "fallback_divisor": 3}
    assert resolve_count_tiers(bad_count, *d) == d
    # bool rejected
    bad_bool = {"tiers": [{"max_players": True, "count": 1}], "fallback_divisor": 3}
    assert resolve_count_tiers(bad_bool, *d) == d


def test_traitor_count_default_characterization():
    """Default tiers reproduce the original 1/2/3/(//3) traitor counts."""
    game = _make_traitor()

    def original(n):
        if n <= 5:
            return 1
        if n <= 8:
            return 2
        if n <= 11:
            return 3
        return n // 3

    for n in (1, 4, 5, 6, 8, 9, 11, 12, 15, 30):
        assert game._get_traitor_count(n) == original(n)


def test_traitor_count_override_frozen():
    custom = {
        "tiers": [{"max_players": 3, "count": 1}, {"max_players": 6, "count": 2}],
        "fallback_divisor": 4,
    }
    with patch("services.game_coordinator.games.traitor.read_object_flag", return_value=custom):
        game = _make_traitor()
    assert game._get_traitor_count(3) == 1
    assert game._get_traitor_count(6) == 2
    assert game._get_traitor_count(12) == 3  # 12 // 4
    # init-frozen: changing the flag after init has no effect
    with patch("services.game_coordinator.games.traitor.read_object_flag", return_value={"tiers": []}):
        assert game._get_traitor_count(3) == 1


# ========================================================================
# Zombie: initial count + respawn window
# ========================================================================


def test_zombie_defaults_reproduce_constants():
    game = _make_zombie()
    assert game.initial_zombies == INITIAL_ZOMBIES
    assert game.zombie_respawn_min == ZOMBIE_RESPAWN_MIN
    assert game.zombie_respawn_max == ZOMBIE_RESPAWN_MAX


def test_zombie_reads_overrides():
    with (
        patch("services.game_coordinator.games.zombie.read_int_flag", return_value=3),
        patch(
            "services.game_coordinator.games.zombie.read_float_flag",
            side_effect=[1.0, 6.0],  # respawn_min, respawn_max
        ),
    ):
        game = _make_zombie()
    assert game.initial_zombies == 3
    assert game.zombie_respawn_min == 1.0
    assert game.zombie_respawn_max == 6.0


def test_zombie_initial_count_malformed_falls_back():
    # non-positive int -> fall back to INITIAL_ZOMBIES
    with patch("services.game_coordinator.games.zombie.read_int_flag", return_value=0):
        game = _make_zombie()
    assert game.initial_zombies == INITIAL_ZOMBIES


def test_zombie_respawn_min_gt_max_falls_back_pair():
    with (
        patch("services.game_coordinator.games.zombie.read_int_flag", return_value=INITIAL_ZOMBIES),
        patch(
            "services.game_coordinator.games.zombie.read_float_flag",
            side_effect=[12.0, 3.0],  # min > max -> both revert
        ),
    ):
        game = _make_zombie()
    assert game.zombie_respawn_min == ZOMBIE_RESPAWN_MIN
    assert game.zombie_respawn_max == ZOMBIE_RESPAWN_MAX
