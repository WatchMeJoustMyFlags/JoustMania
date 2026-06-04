"""
Tests for intervention observability signals added in #730 / #722 §7:
- windowed (rolling) movement variance
- derived skill level (bounds + empty input)

These exercise PlayerAnalytics directly (no game loop) so the computations are
verified against known sequences independently of metric emission.
"""

import math

from services.game_coordinator.games.analytics import (
    MOVEMENT_VARIANCE_WINDOW,
    PlayerAnalytics,
    Playstyle,
)
from services.game_coordinator.runtime_config import AnalyticsConfig


def _make_analytics() -> PlayerAnalytics:
    return PlayerAnalytics(serial="TEST01", game_start_time=0.0)


def _config() -> AnalyticsConfig:
    return AnalyticsConfig()


def _feed(analytics: PlayerAnalytics, magnitudes: list[float], config: AnalyticsConfig) -> None:
    """Record a sequence of accel magnitudes (decomposed onto the x-axis)."""
    for mag in magnitudes:
        analytics.record_sample(
            accel_x=mag,
            accel_y=0.0,
            accel_z=0.0,
            raw_accel_mag=mag,
            smoothed_accel=mag,
            death_threshold=10.0,  # high so nothing counts as near-death
            config=config,
        )


def _sample_variance(values: list[float]) -> float:
    n = len(values)
    mean = sum(values) / n
    return sum((v - mean) ** 2 for v in values) / (n - 1)


class TestWindowedVariance:
    def test_empty_returns_zero(self):
        assert _make_analytics().windowed_variance == 0.0

    def test_single_sample_returns_zero(self):
        analytics = _make_analytics()
        _feed(analytics, [1.0], _config())
        assert analytics.windowed_variance == 0.0

    def test_known_sequence_matches_sample_variance(self):
        analytics = _make_analytics()
        seq = [1.0, 2.0, 3.0, 4.0, 5.0]
        _feed(analytics, seq, _config())
        # Sample variance of 1..5 = 2.5
        assert math.isclose(analytics.windowed_variance, 2.5, rel_tol=1e-9)
        assert math.isclose(analytics.windowed_variance, _sample_variance(seq), rel_tol=1e-9)

    def test_constant_sequence_zero_variance(self):
        analytics = _make_analytics()
        _feed(analytics, [1.2] * 20, _config())
        assert math.isclose(analytics.windowed_variance, 0.0, abs_tol=1e-12)

    def test_window_only_keeps_recent_samples(self):
        analytics = _make_analytics()
        # Fill window with a calm baseline, then push a single window's worth
        # of a different constant. Old samples must be evicted.
        _feed(analytics, [3.0] * MOVEMENT_VARIANCE_WINDOW, _config())
        assert math.isclose(analytics.windowed_variance, 0.0, abs_tol=1e-12)
        _feed(analytics, [1.0] * MOVEMENT_VARIANCE_WINDOW, _config())
        # Window now contains only the 1.0 samples -> variance back to zero
        assert math.isclose(analytics.windowed_variance, 0.0, abs_tol=1e-12)

    def test_window_independent_of_cumulative_welford(self):
        analytics = _make_analytics()
        # A long calm run then a recent jittery burst: cumulative std stays
        # influenced by the whole history, windowed variance reflects only recent.
        _feed(analytics, [1.0] * 200, _config())
        _feed(analytics, [0.5, 2.5] * (MOVEMENT_VARIANCE_WINDOW // 2), _config())
        assert analytics.windowed_variance > 0.5
        # Cumulative std exists but is a separate quantity (sanity: both defined)
        assert analytics.std_deviation >= 0.0


class TestSkillLevel:
    def test_empty_input_returns_zero(self):
        assert _make_analytics().get_skill_level() == 0.0

    def test_always_within_bounds(self):
        config = _config()
        # A spread of behaviors should never escape [0, 1]
        for seq in (
            [1.0] * 50,  # calm
            [1.3] * 50,  # active
            [3.0] * 50,  # aggressive/danger
            [0.5, 5.0] * 25,  # erratic
            [1.2, 1.6, 2.1, 0.9, 1.8] * 10,  # mixed
        ):
            analytics = _make_analytics()
            _feed(analytics, seq, config)
            skill = analytics.get_skill_level()
            assert 0.0 <= skill <= 1.0, f"skill {skill} out of bounds for {seq[:3]}..."

    def test_balanced_player_scores_above_aggressive(self):
        config = _config()
        balanced = _make_analytics()
        # Mostly active zone, low warning/danger -> BALANCED/ACTIVE playstyle
        _feed(balanced, [1.2] * 100, config)

        aggressive = _make_analytics()
        # Mostly danger zone -> AGGRESSIVE playstyle
        _feed(aggressive, [3.0] * 100, config)

        assert aggressive.get_playstyle() == Playstyle.AGGRESSIVE
        assert balanced.get_skill_level() > aggressive.get_skill_level()

    def test_steady_motion_scores_above_erratic_same_playstyle(self):
        config = _config()
        steady = _make_analytics()
        _feed(steady, [1.2] * 100, config)

        erratic = _make_analytics()
        # Same active-ish average but high variance
        _feed(erratic, [0.6, 1.8] * 50, config)

        # Steady has lower windowed variance -> higher smoothness component
        assert steady.windowed_variance < erratic.windowed_variance
        assert steady.get_skill_level() >= erratic.get_skill_level()

    def test_appears_in_summary(self):
        analytics = _make_analytics()
        _feed(analytics, [1.2] * 20, _config())
        summary = analytics.get_summary()
        assert "skill_level" in summary
        assert 0.0 <= summary["skill_level"] <= 1.0
