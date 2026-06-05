"""
Tests for #766 F1: death/warning threshold tables promoted to game flags.

Covers:
- Characterization: flag defaults reproduce the current hardcoded behavior exactly.
- resolve_base_thresholds validation + fallback (wrong length, warning>=max,
  non-numeric, missing keys, non-dict).
- resolve_mode_thresholds validation + fallback for zombie/werewolf overrides.
- Game modes read the flag once at init and store instance threshold tables.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, async_noop

from services.game_coordinator.games.base import (
    FAST_MAX,
    FAST_WARNING,
    SLOW_MAX,
    SLOW_WARNING,
    resolve_base_thresholds,
    resolve_mode_thresholds,
)
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.werewolf import WEREWOLF_THRESHOLDS, WerewolfGame
from services.game_coordinator.games.zombie import ZOMBIE_THRESHOLDS, ZombieGame

# The exact object the game.json default variant carries (must mirror the
# module constants — that is the whole point of behavior-neutral promotion).
GAME_JSON_DEFAULT = {
    "slow_warning": [1.2, 1.3, 1.6, 2.0, 2.5],
    "slow_max": [1.3, 1.5, 1.8, 2.5, 3.2],
    "fast_warning": [1.4, 1.6, 1.9, 2.7, 2.8],
    "fast_max": [1.6, 1.8, 2.8, 3.2, 3.5],
}

ZOMBIE_JSON_DEFAULT = {
    "warning": [1.4, 1.7, 2.1, 2.9, 3.5],
    "max": [1.6, 1.9, 2.6, 3.9, 4.5],
}


# ========================================================================
# resolve_base_thresholds — characterization + validation/fallback
# ========================================================================


def test_game_json_default_matches_module_constants():
    """The flag's default variant must equal the hardcoded module constants."""
    assert GAME_JSON_DEFAULT["slow_warning"] == SLOW_WARNING
    assert GAME_JSON_DEFAULT["slow_max"] == SLOW_MAX
    assert GAME_JSON_DEFAULT["fast_warning"] == FAST_WARNING
    assert GAME_JSON_DEFAULT["fast_max"] == FAST_MAX


def test_default_flag_reproduces_current_behavior():
    """Resolving the game.json default yields exactly the module constants."""
    resolved = resolve_base_thresholds(GAME_JSON_DEFAULT)
    assert resolved["slow_warning"] == SLOW_WARNING
    assert resolved["slow_max"] == SLOW_MAX
    assert resolved["fast_warning"] == FAST_WARNING
    assert resolved["fast_max"] == FAST_MAX


def test_empty_dict_falls_back_to_defaults():
    resolved = resolve_base_thresholds({})
    assert resolved["slow_warning"] == SLOW_WARNING
    assert resolved["fast_max"] == FAST_MAX


def test_non_dict_falls_back_to_defaults():
    for bad in (None, [], "nope", 5):
        resolved = resolve_base_thresholds(bad)
        assert resolved["slow_warning"] == SLOW_WARNING
        assert resolved["slow_max"] == SLOW_MAX


def test_wrong_length_row_falls_back_per_key():
    bad = dict(GAME_JSON_DEFAULT)
    bad["slow_warning"] = [1.0, 1.0, 1.0]  # only 3 entries
    resolved = resolve_base_thresholds(bad)
    # Bad row falls back, others honored
    assert resolved["slow_warning"] == SLOW_WARNING
    assert resolved["fast_warning"] == FAST_WARNING


def test_non_numeric_row_falls_back():
    bad = dict(GAME_JSON_DEFAULT)
    bad["fast_max"] = [1.6, 1.8, "x", 3.2, 3.5]
    resolved = resolve_base_thresholds(bad)
    assert resolved["fast_max"] == FAST_MAX


def test_bool_entries_rejected():
    # bool is a subclass of int; must be rejected as non-numeric threshold.
    bad = dict(GAME_JSON_DEFAULT)
    bad["slow_max"] = [True, False, 1.8, 2.5, 3.2]
    resolved = resolve_base_thresholds(bad)
    assert resolved["slow_max"] == SLOW_MAX


def test_missing_key_falls_back_for_that_key_only():
    partial = {k: v for k, v in GAME_JSON_DEFAULT.items() if k != "fast_warning"}
    resolved = resolve_base_thresholds(partial)
    assert resolved["fast_warning"] == FAST_WARNING  # missing -> default
    assert resolved["slow_warning"] == SLOW_WARNING  # present -> honored


def test_warning_not_below_max_falls_back_pair():
    # slow_warning[2] >= slow_max[2] -> the whole slow pair reverts to defaults.
    bad = {
        "slow_warning": [1.2, 1.3, 5.0, 2.0, 2.5],
        "slow_max": [1.3, 1.5, 1.8, 2.5, 3.2],
        "fast_warning": [1.0, 1.1, 1.2, 1.3, 1.4],
        "fast_max": [2.0, 2.1, 2.2, 2.3, 2.4],
    }
    resolved = resolve_base_thresholds(bad)
    assert resolved["slow_warning"] == SLOW_WARNING
    assert resolved["slow_max"] == SLOW_MAX
    # The valid fast pair is preserved untouched.
    assert resolved["fast_warning"] == [1.0, 1.1, 1.2, 1.3, 1.4]
    assert resolved["fast_max"] == [2.0, 2.1, 2.2, 2.3, 2.4]


def test_custom_valid_override_is_honored():
    custom = {
        "slow_warning": [1.0, 1.1, 1.2, 1.3, 1.4],
        "slow_max": [2.0, 2.1, 2.2, 2.3, 2.4],
        "fast_warning": [1.5, 1.6, 1.7, 1.8, 1.9],
        "fast_max": [3.0, 3.1, 3.2, 3.3, 3.4],
    }
    resolved = resolve_base_thresholds(custom)
    assert resolved == {k: list(v) for k, v in custom.items()}


# ========================================================================
# resolve_mode_thresholds — zombie/werewolf override validation
# ========================================================================


def test_mode_default_reproduces_hardcoded_table():
    resolved = resolve_mode_thresholds(ZOMBIE_JSON_DEFAULT, ZOMBIE_THRESHOLDS)
    assert resolved == ZOMBIE_THRESHOLDS


def test_mode_non_dict_falls_back():
    assert resolve_mode_thresholds(None, WEREWOLF_THRESHOLDS) == WEREWOLF_THRESHOLDS
    assert resolve_mode_thresholds([], WEREWOLF_THRESHOLDS) == WEREWOLF_THRESHOLDS


def test_mode_wrong_length_falls_back():
    bad = {"warning": [1.4, 1.7], "max": [1.6, 1.9, 2.6, 3.9, 4.5]}
    assert resolve_mode_thresholds(bad, ZOMBIE_THRESHOLDS) == ZOMBIE_THRESHOLDS


def test_mode_non_numeric_falls_back():
    bad = {"warning": [1.4, 1.7, "x", 2.9, 3.5], "max": [1.6, 1.9, 2.6, 3.9, 4.5]}
    assert resolve_mode_thresholds(bad, ZOMBIE_THRESHOLDS) == ZOMBIE_THRESHOLDS


def test_mode_missing_key_falls_back():
    bad = {"warning": [1.4, 1.7, 2.1, 2.9, 3.5]}  # no "max"
    assert resolve_mode_thresholds(bad, ZOMBIE_THRESHOLDS) == ZOMBIE_THRESHOLDS


def test_mode_warning_not_below_max_falls_back():
    bad = {"warning": [9.0, 1.7, 2.1, 2.9, 3.5], "max": [1.6, 1.9, 2.6, 3.9, 4.5]}
    assert resolve_mode_thresholds(bad, WEREWOLF_THRESHOLDS) == WEREWOLF_THRESHOLDS


def test_mode_custom_valid_override_honored():
    from services.game_coordinator.games.base import Sensitivity

    custom = {"warning": [1.0, 1.1, 1.2, 1.3, 1.4], "max": [2.0, 2.1, 2.2, 2.3, 2.4]}
    resolved = resolve_mode_thresholds(custom, ZOMBIE_THRESHOLDS)
    assert resolved[Sensitivity.ULTRA_SLOW] == (1.0, 2.0)
    assert resolved[Sensitivity.ULTRA_FAST] == (1.4, 2.4)


# ========================================================================
# Game-mode init reads (flag unavailable in tests -> fallback path)
# ========================================================================


def _make_ffa():
    return FFAGame(
        controller_manager_client=None,
        event_publisher=EventCollector().publish,
        sensitivity=2,
    )


def test_base_init_stores_instance_tables_from_defaults():
    """With no flagd, init falls back to module constants (behavior-neutral)."""
    game = _make_ffa()
    assert game.slow_warning == SLOW_WARNING
    assert game.slow_max == SLOW_MAX
    assert game.fast_warning == FAST_WARNING
    assert game.fast_max == FAST_MAX


def test_base_init_reads_flag_once():
    """The threshold flag is read exactly once at init (init-frozen).

    Base init reads several game-domain object flags (thresholds, F3 windows,
    ...); assert specifically that ``thresholds`` is read exactly once.
    """
    with patch("services.game_coordinator.games.base.read_object_flag", return_value={}) as mock_read:
        _make_ffa()
    threshold_calls = [c for c in mock_read.call_args_list if c.args[1] == "thresholds"]
    assert len(threshold_calls) == 1
    assert threshold_calls[0].args == ("game", "thresholds", {})


def test_base_init_applies_flag_override():
    custom = {
        "slow_warning": [1.0, 1.1, 1.2, 1.3, 1.4],
        "slow_max": [2.0, 2.1, 2.2, 2.3, 2.4],
        "fast_warning": [1.5, 1.6, 1.7, 1.8, 1.9],
        "fast_max": [3.0, 3.1, 3.2, 3.3, 3.4],
    }
    with patch("services.game_coordinator.games.base.read_object_flag", return_value=custom):
        game = _make_ffa()
    assert game.slow_warning == [1.0, 1.1, 1.2, 1.3, 1.4]
    assert game.fast_max == [3.0, 3.1, 3.2, 3.3, 3.4]


def test_compute_thresholds_unchanged_with_default_flag():
    """End-to-end: effective thresholds match the pre-promotion computation."""
    from services.game_coordinator.games.base import Player

    game = _make_ffa()
    game.sensitivity = type(game.sensitivity)(2)  # MEDIUM
    game.music_speed = game.music_speed  # slow default
    player = Player(serial="s", sensitivity_factor=1.0)
    warn, death = game._compute_effective_thresholds(player)
    # At slow music, MEDIUM -> slow tables index 2.
    assert warn == pytest.approx(SLOW_WARNING[2])
    assert death == pytest.approx(SLOW_MAX[2])


def test_zombie_init_reads_override_flag():
    with patch("services.game_coordinator.games.zombie.read_object_flag", return_value={}) as mock_read:
        game = ZombieGame(
            controller_manager_client=None,
            event_publisher=async_noop,
            sensitivity=2,
        )
    assert mock_read.call_args.args == ("game", "zombie.thresholds", {})
    assert game.zombie_thresholds == ZOMBIE_THRESHOLDS


def test_zombie_init_applies_override_flag():
    custom = {"warning": [1.0, 1.1, 1.2, 1.3, 1.4], "max": [2.0, 2.1, 2.2, 2.3, 2.4]}
    with patch("services.game_coordinator.games.zombie.read_object_flag", return_value=custom):
        game = ZombieGame(
            controller_manager_client=None,
            event_publisher=async_noop,
            sensitivity=2,
        )
    from services.game_coordinator.games.base import Sensitivity

    assert game.zombie_thresholds[Sensitivity.MEDIUM] == (1.2, 2.2)


def test_werewolf_init_reads_override_flag():
    with patch("services.game_coordinator.games.werewolf.read_object_flag", return_value={}) as mock_read:
        game = WerewolfGame(
            controller_manager_client=None,
            event_publisher=async_noop,
            sensitivity=2,
        )
    assert mock_read.call_args.args == ("game", "werewolf.thresholds", {})
    assert game.werewolf_thresholds == WEREWOLF_THRESHOLDS


def test_werewolf_init_malformed_override_falls_back():
    bad = {"warning": [1.0, 1.1], "max": [2.0, 2.1, 2.2, 2.3, 2.4]}
    with patch("services.game_coordinator.games.werewolf.read_object_flag", return_value=bad):
        game = WerewolfGame(
            controller_manager_client=None,
            event_publisher=async_noop,
            sensitivity=2,
        )
    assert game.werewolf_thresholds == WEREWOLF_THRESHOLDS
