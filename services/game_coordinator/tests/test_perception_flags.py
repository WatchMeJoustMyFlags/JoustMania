"""Tests for #766 F4: perception thresholds promoted to agent-domain flags.

Covers three guarantees from the issue's consistency rules:

1. Characterization — the flag defaults reproduce the previously hardcoded
   behavior (zone boundaries, playstyle classification, EMA weight).
2. Malformed fallback — invalid flag values fall back to the hardcoded
   defaults so promotion stays behavior-neutral.
3. ``perception.ema_weight`` is read once at game init and frozen for the life
   of the game; changing the flag mid-game has no effect on a running game
   (#722 §5 variance-baseline caveat).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.game_coordinator.games.analytics import PlayerAnalytics, Playstyle
from services.game_coordinator.games.base import BaseGameMode
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.runtime_config import AnalyticsConfig, RuntimeConfigManager

# --- Current hardcoded values (the behavior-neutral defaults) -----------------
DEFAULT_ZONE_STILL_MAX = 1.1
DEFAULT_ZONE_ACTIVE_MAX = 1.5
DEFAULT_ZONE_WARNING_MAX = 2.0
DEFAULT_EMA_WEIGHT = 4.0


# =============================================================================
# agent.json structure
# =============================================================================


def test_agent_json_has_perception_flags_with_default_values():
    """agent.json parses and every perception flag's `default` variant equals the
    current hardcoded value (behavior-neutral promotion)."""
    agent_path = Path(__file__).resolve().parents[3] / "services" / "flagd" / "agent.json"
    data = json.loads(agent_path.read_text())
    flags = data["flags"]

    expected = {
        "perception.zone_still_max": 1.1,
        "perception.zone_active_max": 1.5,
        "perception.zone_warning_max": 2.0,
        "perception.playstyle.aggressive_warning_danger_min": 30,
        "perception.playstyle.calm_still_min": 70,
        "perception.playstyle.calm_warning_danger_max": 10,
        "perception.playstyle.balanced_still_min": 40,
        "perception.playstyle.balanced_warning_danger_max": 20,
        "perception.ema_weight": 4.0,
    }
    for key, value in expected.items():
        assert key in flags, f"missing flag {key}"
        assert flags[key]["variants"]["default"] == value
        assert flags[key]["defaultVariant"] == "default"


# =============================================================================
# Characterization: defaults reproduce current behavior
# =============================================================================


def test_default_zone_boundaries_reproduce_classification():
    """The default zone boundaries classify accel magnitudes into the historical
    zones (still <1.1 < active <1.5 < warning <2.0 < danger)."""
    a = PlayerAnalytics(serial="s", game_start_time=0.0)
    cfg = AnalyticsConfig()
    # Names map to MovementZone members.
    assert a._classify_zone(1.0, cfg).name == "STILL"
    assert a._classify_zone(1.3, cfg).name == "ACTIVE"
    assert a._classify_zone(1.8, cfg).name == "WARNING"
    assert a._classify_zone(2.5, cfg).name == "DANGER"


def test_default_playstyle_thresholds_reproduce_classification():
    """Default playstyle thresholds reproduce the previously hardcoded literals."""
    a = PlayerAnalytics.from_config("s", 0.0, AnalyticsConfig())

    # >30% warning+danger => AGGRESSIVE
    a.time_still_ms, a.time_active_ms, a.time_warning_ms, a.time_danger_ms = 0, 0, 350, 0
    a.time_active_ms = 650
    assert a.get_playstyle() == Playstyle.AGGRESSIVE

    # >70% still, <10% w/d => CALM
    a.time_still_ms, a.time_active_ms, a.time_warning_ms, a.time_danger_ms = 800, 150, 50, 0
    assert a.get_playstyle() == Playstyle.CALM

    # 40-70% still, <20% w/d => BALANCED
    a.time_still_ms, a.time_active_ms, a.time_warning_ms, a.time_danger_ms = 500, 350, 150, 0
    assert a.get_playstyle() == Playstyle.BALANCED

    # otherwise ACTIVE
    a.time_still_ms, a.time_active_ms, a.time_warning_ms, a.time_danger_ms = 300, 550, 150, 0
    assert a.get_playstyle() == Playstyle.ACTIVE


def test_default_ema_weight_reproduces_smoothing_formula():
    """Default EMA weight 4.0 yields the historical (smoothed*4 + raw)/5 formula."""
    player = MagicMock()
    player.smoothed_accel = 1.0
    BaseGameMode._update_ema(player, 2.0, DEFAULT_EMA_WEIGHT)
    # (1.0 * 4 + 2.0) / 5 == 1.2
    assert player.smoothed_accel == 1.2


def test_update_ema_default_weight_matches_legacy_static_call():
    """Calling _update_ema without a weight keeps the legacy weight-4 behavior."""
    player = MagicMock()
    player.smoothed_accel = 1.0
    BaseGameMode._update_ema(player, 2.0)
    assert player.smoothed_accel == 1.2


# =============================================================================
# Malformed fallback
# =============================================================================


def _make_manager_with_agent(agent_values):
    """Build a RuntimeConfigManager whose agent client returns `agent_values`
    (keyed by flag name), and whose other clients echo their defaults."""

    def agent_get_float(key, default, _ctx):
        return agent_values.get(key, default)

    agent_client = MagicMock()
    agent_client.get_float_value.side_effect = agent_get_float

    other_client = MagicMock()
    other_client.get_integer_value.side_effect = lambda _k, d, _c: d

    def get_client(domain):
        return agent_client if domain == "agent" else other_client

    with patch("openfeature.api.add_handler"), patch("lib.feature_flags.get_flag_client", side_effect=get_client):
        return RuntimeConfigManager()


def test_malformed_zone_flags_fall_back_to_defaults():
    """Non-increasing / non-positive zone values are rejected; defaults kept."""
    # active < still (not strictly increasing) and a negative value.
    manager = _make_manager_with_agent(
        {
            "perception.zone_still_max": 1.5,
            "perception.zone_active_max": 1.1,
            "perception.zone_warning_max": -2.0,
        }
    )
    analytics = manager.get_config().analytics
    assert analytics.zone_still_max == DEFAULT_ZONE_STILL_MAX
    assert analytics.zone_active_max == DEFAULT_ZONE_ACTIVE_MAX
    assert analytics.zone_warning_max == DEFAULT_ZONE_WARNING_MAX


def test_malformed_playstyle_flag_falls_back_to_default():
    """An out-of-range playstyle percentage falls back to its default."""
    manager = _make_manager_with_agent(
        {
            "perception.playstyle.aggressive_warning_danger_min": 150.0,  # >100, invalid
            "perception.playstyle.calm_still_min": 65.0,  # valid override
        }
    )
    analytics = manager.get_config().analytics
    assert analytics.playstyle_aggressive_warning_danger_min == 30.0  # default kept
    assert analytics.playstyle_calm_still_min == 65.0  # valid value applied


def test_valid_zone_override_is_applied():
    """A strictly-increasing valid set of zone flags is applied."""
    manager = _make_manager_with_agent(
        {
            "perception.zone_still_max": 0.9,
            "perception.zone_active_max": 1.3,
            "perception.zone_warning_max": 1.8,
        }
    )
    analytics = manager.get_config().analytics
    assert analytics.zone_still_max == 0.9
    assert analytics.zone_active_max == 1.3
    assert analytics.zone_warning_max == 1.8


def test_read_ema_weight_malformed_falls_back_to_default():
    """read_ema_weight returns the default for non-positive / non-finite values."""
    manager = _make_manager_with_agent({"perception.ema_weight": -3.0})
    assert manager.read_ema_weight() == DEFAULT_EMA_WEIGHT


def test_read_ema_weight_valid_override():
    manager = _make_manager_with_agent({"perception.ema_weight": 6.0})
    assert manager.read_ema_weight() == 6.0


def test_read_ema_weight_default_when_no_agent_client():
    manager = RuntimeConfigManager()
    manager.agent_client = None
    assert manager.read_ema_weight() == DEFAULT_EMA_WEIGHT


# =============================================================================
# ema_weight frozen at game init
# =============================================================================


def test_ema_weight_frozen_at_game_init():
    """A running game reads ema_weight ONCE at init; changing the flag mid-game
    does not alter the running game's smoothing weight (#722 §5)."""

    weight_box = {"value": 6.0}

    def fake_read_ema_weight():
        return weight_box["value"]

    manager = MagicMock()
    manager.read_ema_weight.side_effect = fake_read_ema_weight

    with patch("services.game_coordinator.games.base.get_config_manager", return_value=manager):
        game = FFAGame(
            controller_manager_client=MagicMock(),
            event_publisher=MagicMock(),
            game_id="test_frozen_ema",
        )
        # Captured at init.
        assert game._ema_weight == 6.0

        # Simulate a mid-game flag change.
        weight_box["value"] = 2.0

        # The running game still uses the frozen weight: smoothing is computed
        # with weight 6, not the new 2.
        player = MagicMock()
        player.smoothed_accel = 1.0
        BaseGameMode._update_ema(player, 2.0, game._ema_weight)
        # (1.0 * 6 + 2.0) / 7 == 1.142857...
        assert abs(player.smoothed_accel - (6.0 + 2.0) / 7) < 1e-9
        # The manager was consulted exactly once (at init).
        assert manager.read_ema_weight.call_count == 1
