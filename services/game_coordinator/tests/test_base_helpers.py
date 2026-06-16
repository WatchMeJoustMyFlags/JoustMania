"""
Unit tests for extracted helper methods in BaseGameMode (base.py).

Tests the private helpers created during complexity refactoring:
- _compute_accel_magnitude: acceleration vector → scalar magnitude
- _update_ema: exponential moving average filter
- _compute_effective_thresholds: LERP + sensitivity factor
- _record_player_analytics: analytics recording (enabled check)
- _process_gameplay_update: per-update controller processing + win check
- _send_alive_filter_update: alive-set diffing and filter messages
- _update_frame_metrics: frame timing, jitter, dropped frames
- _handle_stream_reconnection: backoff and reconnection logic
- _apply_tempo_change: music tempo transition with state update
"""

import math
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService, async_noop

from lib.types import Sound
from proto import controller_manager_pb2
from services.game_coordinator.games.base import (
    FAST_MAX,
    FAST_MUSIC_SPEED,
    FAST_WARNING,
    MUSIC_TRANSITION_DURATION,
    SLOW_MAX,
    SLOW_MUSIC_SPEED,
    SLOW_WARNING,
    BaseGameMode,
    Player,
)
from services.game_coordinator.games.ffa import FFAGame


class MockGameplayStream:
    """Mock bidirectional stream for testing."""

    def __init__(self):
        self.messages = []

    async def write(self, message):
        self.messages.append(message)


# ========================================================================
# _compute_accel_magnitude
# ========================================================================


class TestComputeAccelMagnitude:
    """Tests for _compute_accel_magnitude static method."""

    def test_zero_vector(self):
        """Zero acceleration should return 0."""
        accel = controller_manager_pb2.Vector3(x=0.0, y=0.0, z=0.0)
        result = BaseGameMode._compute_accel_magnitude(accel)
        assert result == 0.0

    def test_unit_z_axis(self):
        """Standing still (0,0,1) should return ~1.0g."""
        accel = controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0)
        result = BaseGameMode._compute_accel_magnitude(accel)
        assert result == pytest.approx(1.0, abs=1e-9)

    def test_unit_x_axis(self):
        """(1,0,0) should return 1.0."""
        accel = controller_manager_pb2.Vector3(x=1.0, y=0.0, z=0.0)
        result = BaseGameMode._compute_accel_magnitude(accel)
        assert result == pytest.approx(1.0, abs=1e-9)

    def test_known_magnitude(self):
        """(3,4,0) should return 5.0."""
        accel = controller_manager_pb2.Vector3(x=3.0, y=4.0, z=0.0)
        result = BaseGameMode._compute_accel_magnitude(accel)
        assert result == pytest.approx(5.0, abs=1e-9)

    def test_negative_components(self):
        """Negative components should give same magnitude as positive."""
        accel_pos = controller_manager_pb2.Vector3(x=1.0, y=2.0, z=3.0)
        accel_neg = controller_manager_pb2.Vector3(x=-1.0, y=-2.0, z=-3.0)
        assert BaseGameMode._compute_accel_magnitude(accel_pos) == pytest.approx(
            BaseGameMode._compute_accel_magnitude(accel_neg), abs=1e-9
        )

    def test_large_values(self):
        """Large acceleration values should compute correctly."""
        accel = controller_manager_pb2.Vector3(x=100.0, y=100.0, z=100.0)
        expected = math.sqrt(100**2 + 100**2 + 100**2)
        result = BaseGameMode._compute_accel_magnitude(accel)
        assert result == pytest.approx(expected, abs=1e-6)

    def test_very_small_values(self):
        """Very small values should not lose precision."""
        accel = controller_manager_pb2.Vector3(x=0.001, y=0.001, z=0.001)
        expected = math.sqrt(0.001**2 * 3)
        result = BaseGameMode._compute_accel_magnitude(accel)
        assert result == pytest.approx(expected, abs=1e-9)


# ========================================================================
# _update_ema
# ========================================================================


class TestUpdateEma:
    """Tests for _update_ema static method."""

    def test_first_reading_primes_filter(self):
        """Uninitialized player (smoothed_accel~0) should be primed with first reading."""
        player = Player(serial="test", smoothed_accel=0.0)
        BaseGameMode._update_ema(player, 1.5)
        assert player.smoothed_accel == pytest.approx(1.5, abs=1e-9)
        assert player.last_accel_mag == pytest.approx(1.5, abs=1e-9)

    def test_subsequent_reading_applies_ema(self):
        """After priming, EMA formula (prev*4 + new)/5 should apply."""
        player = Player(serial="test", smoothed_accel=1.0)
        BaseGameMode._update_ema(player, 2.0)
        # (1.0 * 4 + 2.0) / 5 = 1.2
        assert player.smoothed_accel == pytest.approx(1.2, abs=1e-9)
        assert player.last_accel_mag == pytest.approx(1.2, abs=1e-9)

    def test_stable_input_converges(self):
        """Repeated identical readings should converge to that value."""
        player = Player(serial="test", smoothed_accel=1.0)
        for _ in range(100):
            BaseGameMode._update_ema(player, 2.0)
        assert player.smoothed_accel == pytest.approx(2.0, abs=0.01)

    def test_ema_weights_previous_heavily(self):
        """EMA should be closer to previous value (80/20 weighting)."""
        player = Player(serial="test", smoothed_accel=1.0)
        BaseGameMode._update_ema(player, 6.0)
        # (1.0 * 4 + 6.0) / 5 = 2.0
        assert player.smoothed_accel == pytest.approx(2.0, abs=1e-9)
        # Should be much closer to 1.0 than to 6.0
        assert abs(player.smoothed_accel - 1.0) < abs(player.smoothed_accel - 6.0)

    def test_zero_accel_mag_primes(self):
        """Zero magnitude reading should still prime the filter."""
        player = Player(serial="test", smoothed_accel=0.0)
        BaseGameMode._update_ema(player, 0.0)
        # smoothed_accel < 1e-9, so it primes with 0.0
        assert player.smoothed_accel == 0.0

    def test_near_zero_threshold(self):
        """Very small smoothed_accel should trigger priming (< 1e-9 check)."""
        player = Player(serial="test", smoothed_accel=1e-10)
        BaseGameMode._update_ema(player, 3.0)
        # Should prime, not EMA
        assert player.smoothed_accel == pytest.approx(3.0, abs=1e-9)


# ========================================================================
# _compute_effective_thresholds
# ========================================================================


class TestComputeEffectiveThresholds:
    """Tests for _compute_effective_thresholds method."""

    @pytest.fixture
    def game(self):
        """Create a minimal FFA game."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        return FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_thresholds",
        )

    def test_slow_music_default_factor(self, game):
        """At slow music with factor=1.0, thresholds should match SLOW_* arrays."""
        from lib.types import Sensitivity

        game.music_speed = SLOW_MUSIC_SPEED
        for sens in Sensitivity:
            game.sensitivity = sens
            player = Player(serial="test", sensitivity_factor=1.0)
            warn, death = game._compute_effective_thresholds(player)
            assert warn == pytest.approx(SLOW_WARNING[sens.value], abs=1e-9)
            assert death == pytest.approx(SLOW_MAX[sens.value], abs=1e-9)

    def test_fast_music_default_factor(self, game):
        """At fast music with factor=1.0, thresholds should match FAST_* arrays."""
        from lib.types import Sensitivity

        game.music_speed = FAST_MUSIC_SPEED
        for sens in Sensitivity:
            game.sensitivity = sens
            player = Player(serial="test", sensitivity_factor=1.0)
            warn, death = game._compute_effective_thresholds(player)
            assert warn == pytest.approx(FAST_WARNING[sens.value], abs=1e-9)
            assert death == pytest.approx(FAST_MAX[sens.value], abs=1e-9)

    def test_mid_music_interpolates(self, game):
        """Mid-tempo should interpolate between slow and fast thresholds."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = (SLOW_MUSIC_SPEED + FAST_MUSIC_SPEED) / 2
        player = Player(serial="test", sensitivity_factor=1.0)
        warn, death = game._compute_effective_thresholds(player)
        idx = Sensitivity.MEDIUM.value
        expected_warn = (SLOW_WARNING[idx] + FAST_WARNING[idx]) / 2
        expected_death = (SLOW_MAX[idx] + FAST_MAX[idx]) / 2
        assert warn == pytest.approx(expected_warn, abs=1e-3)
        assert death == pytest.approx(expected_death, abs=1e-3)

    def test_high_factor_lowers_thresholds(self, game):
        """Factor > 1 should lower thresholds (easier to die)."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        player_default = Player(serial="test", sensitivity_factor=1.0)
        player_high = Player(serial="test", sensitivity_factor=2.0)
        warn_default, death_default = game._compute_effective_thresholds(player_default)
        warn_high, death_high = game._compute_effective_thresholds(player_high)
        assert warn_high < warn_default
        assert death_high < death_default

    def test_low_factor_raises_thresholds(self, game):
        """Factor < 1 (clamped to 0.5) should raise thresholds (harder to die)."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        player_default = Player(serial="test", sensitivity_factor=1.0)
        player_low = Player(serial="test", sensitivity_factor=0.5)
        warn_default, death_default = game._compute_effective_thresholds(player_default)
        warn_low, death_low = game._compute_effective_thresholds(player_low)
        assert warn_low > warn_default
        assert death_low > death_default

    def test_factor_clamped_at_minimum(self, game):
        """Factor below 0.5 should be clamped to 0.5."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        player_below = Player(serial="test", sensitivity_factor=0.1)
        player_at_min = Player(serial="test", sensitivity_factor=0.5)
        warn_below, death_below = game._compute_effective_thresholds(player_below)
        warn_at_min, death_at_min = game._compute_effective_thresholds(player_at_min)
        assert warn_below == pytest.approx(warn_at_min, abs=1e-9)
        assert death_below == pytest.approx(death_at_min, abs=1e-9)

    def test_factor_clamped_at_maximum(self, game):
        """Factor above 2.0 should be clamped to 2.0."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        player_above = Player(serial="test", sensitivity_factor=5.0)
        player_at_max = Player(serial="test", sensitivity_factor=2.0)
        warn_above, death_above = game._compute_effective_thresholds(player_above)
        warn_at_max, death_at_max = game._compute_effective_thresholds(player_at_max)
        assert warn_above == pytest.approx(warn_at_max, abs=1e-9)
        assert death_above == pytest.approx(death_at_max, abs=1e-9)

    # --- #1107: per-player MULTIPLICATIVE handicap_factor ---------------- #

    def test_handicap_neutral_is_noop(self, game):
        """handicap_factor=1.0 (the default) leaves thresholds unchanged."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        base = Player(serial="test", sensitivity_factor=1.0, handicap_factor=1.0)
        warn_b, death_b = game._compute_effective_thresholds(base)
        # Default constructed player has handicap_factor 1.0 too.
        plain = Player(serial="test", sensitivity_factor=1.0)
        warn_p, death_p = game._compute_effective_thresholds(plain)
        assert warn_b == pytest.approx(warn_p) and death_b == pytest.approx(death_p)

    def test_handicap_above_one_raises_threshold(self, game):
        """handicap_factor > 1 MULTIPLIES the threshold up (harder to die / help)."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        neutral = Player(serial="t", sensitivity_factor=1.0, handicap_factor=1.0)
        helped = Player(serial="t", sensitivity_factor=1.0, handicap_factor=1.5)
        warn_n, death_n = game._compute_effective_thresholds(neutral)
        warn_h, death_h = game._compute_effective_thresholds(helped)
        assert warn_h == pytest.approx(warn_n * 1.5)
        assert death_h == pytest.approx(death_n * 1.5)

    def test_handicap_below_one_lowers_threshold(self, game):
        """handicap_factor < 1 lowers the threshold (easier to die / rein in)."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        neutral = Player(serial="t", sensitivity_factor=1.0, handicap_factor=1.0)
        reined = Player(serial="t", sensitivity_factor=1.0, handicap_factor=0.5)
        _, death_n = game._compute_effective_thresholds(neutral)
        _, death_r = game._compute_effective_thresholds(reined)
        assert death_r == pytest.approx(death_n * 0.5)

    def test_handicap_clamped_to_band(self, game):
        """handicap_factor is independently clamped to [0.5, 2.0]."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        above = Player(serial="t", sensitivity_factor=1.0, handicap_factor=5.0)
        at_max = Player(serial="t", sensitivity_factor=1.0, handicap_factor=2.0)
        below = Player(serial="t", sensitivity_factor=1.0, handicap_factor=0.1)
        at_min = Player(serial="t", sensitivity_factor=1.0, handicap_factor=0.5)
        assert game._compute_effective_thresholds(above) == pytest.approx(game._compute_effective_thresholds(at_max))
        assert game._compute_effective_thresholds(below) == pytest.approx(game._compute_effective_thresholds(at_min))

    def test_handicap_composes_multiplicatively_with_sensitivity(self, game):
        """handicap and sensitivity are SEPARATE knobs that compose: a 2.0
        sensitivity (halves) and a 2.0 handicap (doubles) net to neutral."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        neutral = Player(serial="t", sensitivity_factor=1.0, handicap_factor=1.0)
        composed = Player(serial="t", sensitivity_factor=2.0, handicap_factor=2.0)
        warn_n, death_n = game._compute_effective_thresholds(neutral)
        warn_c, death_c = game._compute_effective_thresholds(composed)
        assert warn_c == pytest.approx(warn_n)
        assert death_c == pytest.approx(death_n)

    # --- #1129: time-boxed partial_shield boost in the threshold path ---- #

    def test_partial_shield_active_boosts_threshold(self, game):
        """An active partial shield raises the threshold via its boost."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        neutral = Player(serial="t", sensitivity_factor=1.0, handicap_factor=1.0)
        shielded = Player(
            serial="t",
            sensitivity_factor=1.0,
            handicap_factor=1.0,
            partial_shield_until=time.time() + 5.0,
            partial_shield_boost=2.0,
        )
        _, death_n = game._compute_effective_thresholds(neutral)
        _, death_s = game._compute_effective_thresholds(shielded)
        assert death_s == pytest.approx(death_n * 2.0)

    def test_partial_shield_expired_is_noop(self, game):
        """A partial shield whose deadline has passed leaves thresholds at the
        standing handicap (no reset task needed — it simply stops applying)."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        plain = Player(serial="t", sensitivity_factor=1.0, handicap_factor=1.0)
        expired = Player(
            serial="t",
            sensitivity_factor=1.0,
            handicap_factor=1.0,
            partial_shield_until=time.time() - 1.0,  # already past
            partial_shield_boost=2.0,
        )
        assert game._compute_effective_thresholds(expired) == pytest.approx(game._compute_effective_thresholds(plain))

    def test_partial_shield_takes_max_with_standing_handicap(self, game):
        """While active, the effective handicap is max(handicap_factor, boost):
        a partial shield never WEAKENS a standing set_player_handicap delta."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        # Standing handicap 1.8 (help), weaker shield boost 1.2 -> max keeps 1.8.
        p = Player(
            serial="t",
            sensitivity_factor=1.0,
            handicap_factor=1.8,
            partial_shield_until=time.time() + 5.0,
            partial_shield_boost=1.2,
        )
        ref = Player(serial="t", sensitivity_factor=1.0, handicap_factor=1.8)
        assert game._compute_effective_thresholds(p) == pytest.approx(game._compute_effective_thresholds(ref))

    def test_partial_shield_clamped_not_immune(self, game):
        """Even a maxed boost is clamped to 2.0 — never immune (the point vs
        grant_shield): the boosted death threshold stays finite."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        p = Player(
            serial="t",
            sensitivity_factor=1.0,
            handicap_factor=1.0,
            partial_shield_until=time.time() + 5.0,
            partial_shield_boost=2.0,
        )
        _, death = game._compute_effective_thresholds(p)
        assert death > 0 and death != float("inf")

    # --- #1134: time-boxed soft_penalty TIGHTEN in the threshold path -------- #

    def test_soft_penalty_active_lowers_threshold(self, game):
        """An active tighten WEAKENS the threshold via its factor (< 1.0)."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        neutral = Player(serial="t", sensitivity_factor=1.0, handicap_factor=1.0)
        tightened = Player(
            serial="t",
            sensitivity_factor=1.0,
            handicap_factor=1.0,
            soft_penalty_until=time.time() + 5.0,
            soft_penalty_factor=0.6,
        )
        _, death_n = game._compute_effective_thresholds(neutral)
        _, death_t = game._compute_effective_thresholds(tightened)
        assert death_t == pytest.approx(death_n * 0.6)
        assert death_t < death_n  # easier to die

    def test_soft_penalty_expired_is_noop(self, game):
        """A tighten whose deadline has passed leaves thresholds at the standing
        handicap (no reset task — it simply stops applying)."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        plain = Player(serial="t", sensitivity_factor=1.0, handicap_factor=1.0)
        expired = Player(
            serial="t",
            sensitivity_factor=1.0,
            handicap_factor=1.0,
            soft_penalty_until=time.time() - 1.0,  # already past
            soft_penalty_factor=0.6,
        )
        assert game._compute_effective_thresholds(expired) == pytest.approx(game._compute_effective_thresholds(plain))

    def test_soft_penalty_clamped_not_instant_death(self, game):
        """Even a maxed tighten (0.5) is clamped to the 0.5 lower bound — the
        threshold drops to half, never to zero (never instant-death)."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        neutral = Player(serial="t", sensitivity_factor=1.0, handicap_factor=1.0)
        p = Player(
            serial="t",
            sensitivity_factor=1.0,
            handicap_factor=1.0,
            soft_penalty_until=time.time() + 5.0,
            soft_penalty_factor=0.5,
        )
        _, death_n = game._compute_effective_thresholds(neutral)
        _, death = game._compute_effective_thresholds(p)
        assert death == pytest.approx(death_n * 0.5)
        assert death > 0

    def test_soft_penalty_min_overrides_partial_shield(self, game):
        """Precedence: tighten composes by MIN AFTER partial_shield's MAX, so an
        active tighten cuts through an active partial_shield boost (pressure
        overrides protection). Effective = min(max(1.0, 2.0), 0.6) = 0.6."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        neutral = Player(serial="t", sensitivity_factor=1.0, handicap_factor=1.0)
        both = Player(
            serial="t",
            sensitivity_factor=1.0,
            handicap_factor=1.0,
            partial_shield_until=time.time() + 5.0,
            partial_shield_boost=2.0,
            soft_penalty_until=time.time() + 5.0,
            soft_penalty_factor=0.6,
        )
        _, death_n = game._compute_effective_thresholds(neutral)
        _, death = game._compute_effective_thresholds(both)
        # tighten wins: 0.6, not the shield's 2.0.
        assert death == pytest.approx(death_n * 0.6)

    def test_soft_penalty_min_with_standing_handicap(self, game):
        """A tighten 0.6 against a standing help-handicap 1.8: min(1.8, 0.6) = 0.6,
        so the tighten cuts through the standing help too."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        neutral = Player(serial="t", sensitivity_factor=1.0, handicap_factor=1.0)
        p = Player(
            serial="t",
            sensitivity_factor=1.0,
            handicap_factor=1.8,
            soft_penalty_until=time.time() + 5.0,
            soft_penalty_factor=0.6,
        )
        _, death_n = game._compute_effective_thresholds(neutral)
        _, death = game._compute_effective_thresholds(p)
        assert death == pytest.approx(death_n * 0.6)

    # --- #1143: time-boxed auto_rubberband BOOST in the threshold path ------- #

    def test_rubberband_active_boosts_threshold(self, game):
        """An active rubberband boost (>= 1.0) raises the threshold via MAX."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        neutral = Player(serial="t", sensitivity_factor=1.0, handicap_factor=1.0)
        boosted = Player(
            serial="t",
            sensitivity_factor=1.0,
            handicap_factor=1.0,
            rubberband_until=time.time() + 5.0,
            rubberband_boost=1.4,
        )
        _, death_n = game._compute_effective_thresholds(neutral)
        _, death_b = game._compute_effective_thresholds(boosted)
        assert death_b == pytest.approx(death_n * 1.4)
        assert death_b > death_n  # harder to die

    def test_rubberband_expired_is_noop(self, game):
        """A rubberband whose deadline has passed simply stops applying."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        plain = Player(serial="t", sensitivity_factor=1.0, handicap_factor=1.0)
        expired = Player(
            serial="t",
            sensitivity_factor=1.0,
            handicap_factor=1.0,
            rubberband_until=time.time() - 1.0,  # already past
            rubberband_boost=1.4,
        )
        assert game._compute_effective_thresholds(expired) == pytest.approx(game._compute_effective_thresholds(plain))

    def test_rubberband_takes_max_with_partial_shield(self, game):
        """Both strengthening boosts compose by MAX: a stronger partial_shield
        boost wins over a weaker rubberband boost (and vice versa)."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        neutral = Player(serial="t", sensitivity_factor=1.0, handicap_factor=1.0)
        both = Player(
            serial="t",
            sensitivity_factor=1.0,
            handicap_factor=1.0,
            partial_shield_until=time.time() + 5.0,
            partial_shield_boost=1.2,
            rubberband_until=time.time() + 5.0,
            rubberband_boost=1.4,
        )
        _, death_n = game._compute_effective_thresholds(neutral)
        _, death = game._compute_effective_thresholds(both)
        # MAX(1.2, 1.4) = 1.4 (the stronger boost wins).
        assert death == pytest.approx(death_n * 1.4)

    def test_rubberband_soft_penalty_min_overrides(self, game):
        """soft_penalty MIN is applied AFTER both boosts' MAX, so an active tighten
        cuts through a rubberband boost too (pressure overrides protection)."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        neutral = Player(serial="t", sensitivity_factor=1.0, handicap_factor=1.0)
        both = Player(
            serial="t",
            sensitivity_factor=1.0,
            handicap_factor=1.0,
            rubberband_until=time.time() + 5.0,
            rubberband_boost=1.4,
            soft_penalty_until=time.time() + 5.0,
            soft_penalty_factor=0.6,
        )
        _, death_n = game._compute_effective_thresholds(neutral)
        _, death = game._compute_effective_thresholds(both)
        assert death == pytest.approx(death_n * 0.6)

    def test_rubberband_clamped_not_immune(self, game):
        """Even a maxed boost is clamped to 2.0 — never immune, never infinite."""
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        p = Player(
            serial="t",
            sensitivity_factor=1.0,
            handicap_factor=1.0,
            rubberband_until=time.time() + 5.0,
            rubberband_boost=2.0,
        )
        _, death = game._compute_effective_thresholds(p)
        assert death > 0 and death != float("inf")


# ========================================================================
# grant_partial_shield primitive (#1129)
# ========================================================================


class TestGrantPartialShield:
    """Tests for the BaseGameMode.grant_partial_shield time-boxed primitive."""

    @pytest.fixture
    def game(self):
        mock_cm = MockControllerManagerService(num_controllers=2)
        game = FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_partial_shield",
        )
        game.players["AA"] = Player(serial="AA", alive=True)
        return game

    @pytest.mark.asyncio
    async def test_arms_boost_and_deadline(self, game):
        ok = await game.grant_partial_shield("AA", 5.0, 1.8)
        assert ok is True
        p = game.players["AA"]
        assert p.partial_shield_boost == pytest.approx(1.8)
        assert p.partial_shield_until > time.time()

    @pytest.mark.asyncio
    async def test_default_boost_when_omitted(self, game):
        await game.grant_partial_shield("AA", 5.0)
        assert game.players["AA"].partial_shield_boost == pytest.approx(game.PARTIAL_SHIELD_DEFAULT_BOOST)

    @pytest.mark.asyncio
    async def test_boost_clamped_to_band(self, game):
        await game.grant_partial_shield("AA", 5.0, 9.0)
        assert game.players["AA"].partial_shield_boost == pytest.approx(game.PARTIAL_SHIELD_BOOST_MAX)
        await game.grant_partial_shield("AA", 5.0, 0.1)
        # extend-not-shorten / strengthen-not-weaken keeps the higher (max) boost.
        assert game.players["AA"].partial_shield_boost == pytest.approx(game.PARTIAL_SHIELD_BOOST_MAX)

    @pytest.mark.asyncio
    async def test_seconds_capped(self, game):
        await game.grant_partial_shield("AA", 9999.0)
        assert game.players["AA"].partial_shield_until <= time.time() + game.PARTIAL_SHIELD_MAX_SECONDS + 1.0

    @pytest.mark.asyncio
    async def test_nonpositive_seconds_noop(self, game):
        assert await game.grant_partial_shield("AA", 0.0) is False
        assert await game.grant_partial_shield("AA", -3.0) is False
        assert game.players["AA"].partial_shield_until == 0.0

    @pytest.mark.asyncio
    async def test_unknown_or_dead_player_noop(self, game):
        assert await game.grant_partial_shield("ZZ", 5.0) is False
        game.players["AA"].alive = False
        assert await game.grant_partial_shield("AA", 5.0) is False

    @pytest.mark.asyncio
    async def test_extend_not_shorten(self, game):
        await game.grant_partial_shield("AA", 30.0, 2.0)
        far = game.players["AA"].partial_shield_until
        # A shorter, weaker re-arming must not reduce the active window/boost.
        await game.grant_partial_shield("AA", 1.0, 1.1)
        assert game.players["AA"].partial_shield_until == pytest.approx(far)
        assert game.players["AA"].partial_shield_boost == pytest.approx(2.0)


# ========================================================================
# soft_penalty primitives: warn_player + apply_soft_penalty (#1134)
# ========================================================================


class TestSoftPenaltyPrimitives:
    """Tests for warn_player (warn) and apply_soft_penalty (tighten)."""

    @pytest.fixture
    def game(self):
        mock_cm = MockControllerManagerService(num_controllers=2)
        game = FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_soft_penalty",
        )
        game.players["AA"] = Player(serial="AA", alive=True)
        return game

    # --- warn: feedback only, NO threshold change --------------------------- #

    @pytest.mark.asyncio
    async def test_warn_fires_feedback_no_threshold_change(self, game):
        from lib.types import Sensitivity

        game.sensitivity = Sensitivity.MEDIUM
        before = game._compute_effective_thresholds(game.players["AA"])
        ok = await game.warn_player("AA")
        assert ok is True
        # warn is purely visual/haptic: warning_until set, thresholds untouched.
        assert game.players["AA"].warning_until > time.time()
        assert game.players["AA"].soft_penalty_until == 0.0
        after = game._compute_effective_thresholds(game.players["AA"])
        assert before == pytest.approx(after)

    @pytest.mark.asyncio
    async def test_warn_unknown_or_dead_noop(self, game):
        assert await game.warn_player("ZZ") is False
        game.players["AA"].alive = False
        assert await game.warn_player("AA") is False

    # --- tighten: expiring handicap reduction ------------------------------- #

    @pytest.mark.asyncio
    async def test_tighten_arms_factor_and_deadline(self, game):
        ok = await game.apply_soft_penalty("AA", 5.0, 0.6)
        assert ok is True
        p = game.players["AA"]
        assert p.soft_penalty_factor == pytest.approx(0.6)
        assert p.soft_penalty_until > time.time()

    @pytest.mark.asyncio
    async def test_tighten_default_factor_when_omitted(self, game):
        await game.apply_soft_penalty("AA", 5.0)
        assert game.players["AA"].soft_penalty_factor == pytest.approx(game.SOFT_PENALTY_DEFAULT_FACTOR)

    @pytest.mark.asyncio
    async def test_tighten_factor_clamped_to_band(self, game):
        # Below the floor clamps up to 0.5 (never instant-death).
        await game.apply_soft_penalty("AA", 5.0, 0.1)
        assert game.players["AA"].soft_penalty_factor == pytest.approx(game.SOFT_PENALTY_FACTOR_MIN)
        # Above 1.0 clamps to 1.0 (a no-op weakening) — but tighten-not-loosen
        # keeps the stronger (lower) factor from the first arming.
        await game.apply_soft_penalty("AA", 5.0, 9.0)
        assert game.players["AA"].soft_penalty_factor == pytest.approx(game.SOFT_PENALTY_FACTOR_MIN)

    @pytest.mark.asyncio
    async def test_tighten_seconds_capped(self, game):
        await game.apply_soft_penalty("AA", 9999.0)
        assert game.players["AA"].soft_penalty_until <= time.time() + game.SOFT_PENALTY_MAX_SECONDS + 1.0

    @pytest.mark.asyncio
    async def test_tighten_nonpositive_seconds_noop(self, game):
        assert await game.apply_soft_penalty("AA", 0.0) is False
        assert await game.apply_soft_penalty("AA", -3.0) is False
        assert game.players["AA"].soft_penalty_until == 0.0

    @pytest.mark.asyncio
    async def test_tighten_unknown_or_dead_player_noop(self, game):
        assert await game.apply_soft_penalty("ZZ", 5.0) is False
        game.players["AA"].alive = False
        assert await game.apply_soft_penalty("AA", 5.0) is False

    @pytest.mark.asyncio
    async def test_tighten_extend_not_shorten_strengthen_not_loosen(self, game):
        await game.apply_soft_penalty("AA", 30.0, 0.5)
        far = game.players["AA"].soft_penalty_until
        # A shorter, WEAKER (higher factor) re-arming must not reduce the active
        # window or loosen the active factor.
        await game.apply_soft_penalty("AA", 1.0, 0.9)
        assert game.players["AA"].soft_penalty_until == pytest.approx(far)
        assert game.players["AA"].soft_penalty_factor == pytest.approx(0.5)


# ========================================================================
# apply_auto_rubberband primitive (#1143)
# ========================================================================


class TestApplyAutoRubberband:
    """Tests for BaseGameMode.apply_auto_rubberband: single decision the
    coordinator EXPANDS across players from the live skill gap."""

    @pytest.fixture
    def game(self):
        from lib.types import Sensitivity

        mock_cm = MockControllerManagerService(num_controllers=3)
        game = FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_rubberband",
        )
        game.sensitivity = Sensitivity.MEDIUM
        game.music_speed = SLOW_MUSIC_SPEED
        # LEADER: low intensity => large headroom => comfortably ahead.
        game.players["LEAD"] = Player(serial="LEAD", alive=True, smoothed_accel=0.2)
        # LAGGARD: high intensity => small headroom => on the edge.
        game.players["LAG"] = Player(serial="LAG", alive=True, smoothed_accel=1.5)
        return game

    @pytest.mark.asyncio
    async def test_unknown_strength_noop(self, game):
        assert await game.apply_auto_rubberband("off") == 0
        assert await game.apply_auto_rubberband("aggressive") == 0
        assert await game.apply_auto_rubberband("") == 0
        for p in game.players.values():
            assert p.rubberband_until == 0.0

    @pytest.mark.asyncio
    async def test_fewer_than_two_players_noop(self, game):
        game.players = {"solo": Player(serial="solo", alive=True, smoothed_accel=1.0)}
        assert await game.apply_auto_rubberband("strong") == 0

    @pytest.mark.asyncio
    async def test_boosts_laggard_not_leader(self, game):
        n = await game.apply_auto_rubberband("strong")
        assert n >= 1
        lag = game.players["LAG"]
        lead = game.players["LEAD"]
        # The laggard (most behind) gets the boost.
        assert lag.rubberband_until > time.time()
        assert lag.rubberband_boost > 1.0
        # The leader gets (close to) nothing — never penalized.
        assert lead.rubberband_boost == pytest.approx(1.0) or lead.rubberband_until <= time.time()

    @pytest.mark.asyncio
    async def test_strong_boosts_more_than_gentle(self, game):
        await game.apply_auto_rubberband("gentle")
        gentle_boost = game.players["LAG"].rubberband_boost
        # Fresh game so the strong boost isn't capped by extend-not-weaken.
        game.players["LAG"].rubberband_until = 0.0
        game.players["LAG"].rubberband_boost = 1.0
        await game.apply_auto_rubberband("strong")
        strong_boost = game.players["LAG"].rubberband_boost
        assert strong_boost > gentle_boost

    @pytest.mark.asyncio
    async def test_gentle_cap_respected(self, game):
        await game.apply_auto_rubberband("gentle")
        # Gentle tops out at +0.2 above neutral (the laggard is the most behind).
        assert game.players["LAG"].rubberband_boost <= 1.0 + game.RUBBERBAND_MAX_BOOST_GENTLE + 1e-9

    @pytest.mark.asyncio
    async def test_never_inverts_standings(self, game):
        """Standings = headroom to death. The boosted laggard's resulting headroom
        must never exceed the leader's (compression only, never overtaking)."""
        lead = game.players["LEAD"]
        _, lead_death_before = game._compute_effective_thresholds(lead)
        lead_headroom = lead_death_before - lead.smoothed_accel

        await game.apply_auto_rubberband("strong")

        lag = game.players["LAG"]
        _, lag_death = game._compute_effective_thresholds(lag)
        lag_headroom = lag_death - lag.smoothed_accel
        assert lag_headroom <= lead_headroom + 1e-6
        # The leader (LEAD) is never penalized; its headroom is unchanged.
        _, lead_death_after = game._compute_effective_thresholds(lead)
        assert lead_death_after == pytest.approx(lead_death_before)

    @pytest.mark.asyncio
    async def test_never_instant_death_threshold_finite(self, game):
        await game.apply_auto_rubberband("strong")
        for p in game.players.values():
            _, death = game._compute_effective_thresholds(p)
            assert death > 0 and death != float("inf")

    @pytest.mark.asyncio
    async def test_equal_field_noop(self, game):
        """If everyone has identical headroom there is no gap to compress."""
        for p in game.players.values():
            p.smoothed_accel = 0.8
        assert await game.apply_auto_rubberband("strong") == 0

    @pytest.mark.asyncio
    async def test_seconds_capped(self, game):
        await game.apply_auto_rubberband("strong", seconds=9999.0)
        assert game.players["LAG"].rubberband_until <= time.time() + game.RUBBERBAND_MAX_SECONDS + 1.0

    @pytest.mark.asyncio
    async def test_nonpositive_seconds_noop(self, game):
        assert await game.apply_auto_rubberband("strong", seconds=0.0) == 0
        assert await game.apply_auto_rubberband("strong", seconds=-5.0) == 0

    @pytest.mark.asyncio
    async def test_extend_not_shorten_strengthen_not_weaken(self, game):
        await game.apply_auto_rubberband("strong", seconds=30.0)
        far = game.players["LAG"].rubberband_until
        strong_boost = game.players["LAG"].rubberband_boost
        # A shorter, weaker (gentle) re-arming must not reduce the active window
        # or weaken the active boost.
        await game.apply_auto_rubberband("gentle", seconds=1.0)
        assert game.players["LAG"].rubberband_until == pytest.approx(far)
        assert game.players["LAG"].rubberband_boost == pytest.approx(strong_boost)


# ========================================================================
# _process_controller_state (player name capture)
# ========================================================================


class TestPlayerNameCapture:
    """Tests for player name capture from gameplay data."""

    @pytest.fixture
    def game(self):
        """Create game with a player for name capture tests."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        game = FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_name",
        )
        game.gameplay_stream = MockGameplayStream()
        game.running = True
        game.music_speed = SLOW_MUSIC_SPEED
        game.start_time = time.time()
        game.players["p1"] = Player(
            serial="p1",
            alive=True,
            color=(255, 0, 0),
            smoothed_accel=1.0,
        )
        game.players["p2"] = Player(
            serial="p2",
            alive=True,
            color=(0, 255, 0),
            smoothed_accel=1.0,
        )
        return game

    @pytest.mark.asyncio
    async def test_captures_name_from_first_frame(self, game):
        """Should store controller name on first gameplay data with name."""
        controller_state = controller_manager_pb2.GameplayData(
            serial="p1",
            name="Blue Phoenix",
            accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0),
        )
        await game._process_controller_state(controller_state)
        assert game.players["p1"].name == "Blue Phoenix"

    @pytest.mark.asyncio
    async def test_updates_span_name(self, game):
        """Should call update_name on player span with controller name."""
        mock_span = MagicMock()
        game.players["p1"].span = mock_span
        controller_state = controller_manager_pb2.GameplayData(
            serial="p1",
            name="Swift Tiger",
            accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0),
        )
        await game._process_controller_state(controller_state)
        mock_span.update_name.assert_called_once_with("player: Swift Tiger")
        mock_span.set_attribute.assert_any_call("player.name", "Swift Tiger")

    @pytest.mark.asyncio
    async def test_name_captured_only_once(self, game):
        """Should not overwrite name on subsequent frames."""
        game.players["p1"].name = "Blue Phoenix"
        mock_span = MagicMock()
        game.players["p1"].span = mock_span
        controller_state = controller_manager_pb2.GameplayData(
            serial="p1",
            name="Different Name",
            accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0),
        )
        await game._process_controller_state(controller_state)
        assert game.players["p1"].name == "Blue Phoenix"
        mock_span.update_name.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_name_not_captured(self, game):
        """Should not capture empty name string."""
        controller_state = controller_manager_pb2.GameplayData(
            serial="p1",
            name="",
            accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0),
        )
        await game._process_controller_state(controller_state)
        assert game.players["p1"].name == ""


# ========================================================================
# _process_gameplay_update
# ========================================================================


class TestProcessGameplayUpdate:
    """Tests for _process_gameplay_update method."""

    @pytest.fixture
    def game(self):
        """Create game with players for gameplay update tests."""
        mock_cm = MockControllerManagerService(num_controllers=3)
        event_collector = EventCollector()
        game = FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_update",
        )
        game.gameplay_stream = MockGameplayStream()
        game.running = True
        game.music_speed = SLOW_MUSIC_SPEED
        game.start_time = time.time()

        for i in range(3):
            game.players[f"player_{i}"] = Player(
                serial=f"player_{i}",
                team=0,
                alive=True,
                color=(255, 255, 255),
                smoothed_accel=1.0,
            )
        return game, event_collector

    @pytest.mark.asyncio
    async def test_no_win_returns_false(self, game):
        """Safe movement should not trigger game over."""
        game_instance, _ = game
        update = controller_manager_pb2.GameplayDataUpdate(
            controllers=[
                controller_manager_pb2.GameplayData(
                    serial="player_0",
                    accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0),
                ),
            ],
            timestamp=int(time.time() * 1000),
        )
        result = await game_instance._process_gameplay_update(update)
        assert result is False

    @pytest.mark.asyncio
    async def test_win_condition_returns_true(self, game):
        """When enough players die, should return True."""
        game_instance, _ = game
        # Kill all but one player manually
        game_instance.players["player_0"].alive = False
        game_instance.players["player_1"].alive = False

        # Process remaining player with safe data
        update = controller_manager_pb2.GameplayDataUpdate(
            controllers=[
                controller_manager_pb2.GameplayData(
                    serial="player_2",
                    accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0),
                ),
            ],
            timestamp=int(time.time() * 1000),
        )
        result = await game_instance._process_gameplay_update(update)
        assert result is True

    @pytest.mark.asyncio
    async def test_empty_update_returns_false(self, game):
        """Empty controller list should return False (no game over)."""
        game_instance, _ = game
        update = controller_manager_pb2.GameplayDataUpdate(
            controllers=[],
            timestamp=int(time.time() * 1000),
        )
        result = await game_instance._process_gameplay_update(update)
        assert result is False


# ========================================================================
# _send_alive_filter_update
# ========================================================================


class TestSendAliveFilterUpdate:
    """Tests for _send_alive_filter_update method."""

    @pytest.fixture
    def game(self):
        """Create game with players for filter tests."""
        mock_cm = MockControllerManagerService(num_controllers=3)
        game = FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_filter",
        )
        game.gameplay_stream = MockGameplayStream()
        game.running = True

        for i in range(3):
            game.players[f"player_{i}"] = Player(
                serial=f"player_{i}",
                team=0,
                alive=True,
                color=(255, 255, 255),
            )
        return game

    @pytest.mark.asyncio
    async def test_no_change_no_message(self, game):
        """When alive set unchanged, no filter message should be sent."""
        all_alive = {"player_0", "player_1", "player_2"}
        stream = game.gameplay_stream
        messages_before = len(stream.messages)

        result = await game._send_alive_filter_update(all_alive)

        assert result == all_alive
        assert len(stream.messages) == messages_before

    @pytest.mark.asyncio
    async def test_player_died_sends_filter(self, game):
        """When a player dies, filter update should be sent."""
        game.players["player_1"].alive = False
        old_alive = {"player_0", "player_1", "player_2"}
        stream = game.gameplay_stream
        messages_before = len(stream.messages)

        result = await game._send_alive_filter_update(old_alive)

        assert result == {"player_0", "player_2"}
        assert len(stream.messages) == messages_before + 1

    @pytest.mark.asyncio
    async def test_returns_current_alive_set(self, game):
        """Should always return the current alive set."""
        game.players["player_0"].alive = False
        game.players["player_2"].alive = False
        old_alive = {"player_0", "player_1", "player_2"}

        result = await game._send_alive_filter_update(old_alive)

        assert result == {"player_1"}


# ========================================================================
# _update_frame_metrics
# ========================================================================


class TestUpdateFrameMetrics:
    """Tests for _update_frame_metrics method."""

    @pytest.fixture
    def game(self):
        """Create a minimal FFA game."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        return FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_metrics",
        )

    def test_on_target_frame_increments_count(self, game):
        """Frame within 1.5x target should increment on-target count."""
        target = 16.67  # ~60Hz
        recent = []
        result = game._update_frame_metrics(
            iteration_latency_ms=15.0,
            target_frame_time_ms=target,
            recent_frame_times=recent,
            frames_on_target=0,
            loop_iterations=1,
            loop_start_time=time.time() - 1.0,
        )
        assert result == 1

    def test_slow_frame_does_not_increment(self, game):
        """Frame exceeding 1.5x target should not increment on-target count."""
        target = 16.67
        recent = []
        result = game._update_frame_metrics(
            iteration_latency_ms=30.0,  # > 16.67 * 1.5 = 25.0
            target_frame_time_ms=target,
            recent_frame_times=recent,
            frames_on_target=5,
            loop_iterations=1,
            loop_start_time=time.time() - 1.0,
        )
        assert result == 5  # unchanged

    def test_recent_frame_times_capped_at_60(self, game):
        """Recent frame times list should not exceed 60 entries."""
        target = 16.67
        recent = [16.0] * 60  # Already at capacity
        game._update_frame_metrics(
            iteration_latency_ms=17.0,
            target_frame_time_ms=target,
            recent_frame_times=recent,
            frames_on_target=0,
            loop_iterations=1,
            loop_start_time=time.time() - 1.0,
        )
        assert len(recent) == 60
        assert recent[-1] == 17.0  # New value at end

    def test_recent_frame_times_grows_under_60(self, game):
        """Recent frame times should grow until reaching 60."""
        target = 16.67
        recent = []
        game._update_frame_metrics(
            iteration_latency_ms=16.0,
            target_frame_time_ms=target,
            recent_frame_times=recent,
            frames_on_target=0,
            loop_iterations=1,
            loop_start_time=time.time() - 1.0,
        )
        assert len(recent) == 1
        assert recent[0] == 16.0

    def test_accumulates_on_target_count(self, game):
        """On-target count should accumulate across calls."""
        target = 16.67
        recent = []
        count = 0
        for _ in range(5):
            count = game._update_frame_metrics(
                iteration_latency_ms=15.0,
                target_frame_time_ms=target,
                recent_frame_times=recent,
                frames_on_target=count,
                loop_iterations=1,
                loop_start_time=time.time() - 1.0,
            )
        assert count == 5


# ========================================================================
# _handle_stream_reconnection
# ========================================================================


class TestHandleStreamReconnection:
    """Tests for _handle_stream_reconnection method."""

    @pytest.fixture
    def game(self):
        """Create game for reconnection tests."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        game = FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_reconnect",
        )
        game.gameplay_stream = MockGameplayStream()
        game.running = True
        game.players["p1"] = Player(serial="p1", alive=True, color=(255, 0, 0))
        return game

    @pytest.mark.asyncio
    async def test_within_max_attempts_reconnects(self, game):
        """Should reconnect and return attempt number when within limit."""
        error = RuntimeError("stream broken")
        # Mock _create_gameplay_stream to avoid real gRPC
        game._create_gameplay_stream = AsyncMock()

        result = await game._handle_stream_reconnection(error, attempt=1, max_attempts=3)

        assert result == 1
        game._create_gameplay_stream.assert_called_once()

    @pytest.mark.asyncio
    async def test_exceeds_max_attempts_raises(self, game):
        """Should raise when attempt exceeds max."""
        error = RuntimeError("stream broken")
        game._create_gameplay_stream = AsyncMock()

        with pytest.raises(RuntimeError, match="stream broken"):
            await game._handle_stream_reconnection(error, attempt=4, max_attempts=3)

    @pytest.mark.asyncio
    async def test_at_max_attempts_still_reconnects(self, game):
        """Should still reconnect at exactly max attempts."""
        error = RuntimeError("stream broken")
        game._create_gameplay_stream = AsyncMock()

        result = await game._handle_stream_reconnection(error, attempt=3, max_attempts=3)
        assert result == 3
        game._create_gameplay_stream.assert_called_once()


# ========================================================================
# _apply_tempo_change
# ========================================================================


class TestApplyTempoChange:
    """Tests for _apply_tempo_change method."""

    @pytest.fixture
    def game(self):
        """Create game with mock audio client for tempo tests."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_audio = AsyncMock()
        game = FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=mock_audio,
            game_id="test_tempo",
        )
        game.music_track_id = "track_123"
        game.music_speed = SLOW_MUSIC_SPEED
        game.speed_up = True
        game.players["p1"] = Player(serial="p1", alive=True, color=(255, 0, 0))
        game.players["p2"] = Player(serial="p2", alive=True, color=(0, 255, 0))
        game.dead_count = 0
        game.gameplay_span = None
        game.gameplay_span_context = None
        return game

    @pytest.mark.asyncio
    async def test_updates_music_speed(self, game):
        """Should update music_speed to target tempo."""
        await game._apply_tempo_change(FAST_MUSIC_SPEED)
        assert game.music_speed == FAST_MUSIC_SPEED

    @pytest.mark.asyncio
    async def test_toggles_speed_up(self, game):
        """Should toggle speed_up flag."""
        assert game.speed_up is True
        await game._apply_tempo_change(FAST_MUSIC_SPEED)
        assert game.speed_up is False

    @pytest.mark.asyncio
    async def test_schedules_next_change(self, game):
        """Should set change_time to a future timestamp."""
        before = time.time()
        await game._apply_tempo_change(FAST_MUSIC_SPEED)
        assert game.change_time > before

    @pytest.mark.asyncio
    async def test_calls_audio_client(self, game):
        """Should call ChangeTempo on audio client."""
        await game._apply_tempo_change(FAST_MUSIC_SPEED)
        game.audio_client.ChangeTempo.assert_called_once()
        call_args = game.audio_client.ChangeTempo.call_args[0][0]
        assert call_args.track_id == "track_123"
        assert call_args.new_tempo == pytest.approx(FAST_MUSIC_SPEED, abs=1e-6)
        assert call_args.transition_duration == pytest.approx(MUSIC_TRANSITION_DURATION, abs=1e-6)

    @pytest.mark.asyncio
    async def test_slow_down_from_fast(self, game):
        """Should handle slowing down from fast tempo."""
        game.music_speed = FAST_MUSIC_SPEED
        game.speed_up = False
        await game._apply_tempo_change(SLOW_MUSIC_SPEED)
        assert game.music_speed == SLOW_MUSIC_SPEED
        assert game.speed_up is True  # Toggled back

    @pytest.mark.asyncio
    async def test_creates_child_span(self, game):
        """Should create a music_tempo_change child span wrapping the gRPC call."""
        # The span is created by tracer.start_as_current_span inside _apply_tempo_change.
        # Verify the method completes successfully (span creation is internal).
        await game._apply_tempo_change(FAST_MUSIC_SPEED)
        game.audio_client.ChangeTempo.assert_called_once()

    @pytest.mark.asyncio
    async def test_adds_span_event_when_span_exists(self, game):
        """Should add timeline marker event on gameplay_span."""
        mock_span = MagicMock()
        game.gameplay_span = mock_span
        await game._apply_tempo_change(FAST_MUSIC_SPEED)
        mock_span.add_event.assert_called_once_with(
            "music_tempo_change",
            attributes={
                "old_tempo": SLOW_MUSIC_SPEED,
                "new_tempo": FAST_MUSIC_SPEED,
                "direction": "speed_up",
            },
        )

    @pytest.mark.asyncio
    async def test_no_span_event_when_no_span(self, game):
        """Should not crash when gameplay_span is None."""
        game.gameplay_span = None
        await game._apply_tempo_change(FAST_MUSIC_SPEED)
        game.audio_client.ChangeTempo.assert_called_once()


# ========================================================================
# _play_sound (game_cycle context)
# ========================================================================


class TestPlaySound:
    """Tests for _play_sound method with game_cycle context."""

    @pytest.fixture
    def game(self):
        """Create game with mock audio client."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_audio = AsyncMock()
        return FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=mock_audio,
            game_id="test_sound",
        )

    @pytest.mark.asyncio
    async def test_calls_audio_client(self, game):
        """Should call PlaySound on audio client."""
        await game._play_sound(Sound.SFX_BEEP_LOUD, priority=2)
        game.audio_client.PlaySound.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_crash_without_audio_client(self, game):
        """Should return early when audio_client is None."""
        game.audio_client = None
        await game._play_sound(Sound.SFX_BEEP_LOUD)  # Should not raise

    @pytest.mark.asyncio
    async def test_uses_game_cycle_context_when_set(self, game):
        """Should attach game_cycle_context for sound calls when available."""
        # Set a mock game_cycle_context
        game.game_cycle_context = MagicMock()
        await game._play_sound(Sound.SFX_EXPLOSION, priority=2)
        game.audio_client.PlaySound.assert_called_once()

    @pytest.mark.asyncio
    async def test_falls_back_without_game_cycle_context(self, game):
        """Should call PlaySound normally when game_cycle_context is None."""
        game.game_cycle_context = None
        await game._play_sound(Sound.SFX_EXPLOSION, priority=2)
        game.audio_client.PlaySound.assert_called_once()


# ========================================================================
# _close_grouping_spans
# ========================================================================


class TestCloseGroupingSpans:
    """Tests for _close_grouping_spans method."""

    @pytest.fixture
    def game(self):
        """Create a minimal FFA game."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        return FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_grouping",
        )

    def test_closes_players_span(self, game):
        """Should end and clear _players_span."""
        mock_span = MagicMock()
        game._players_span = mock_span
        game._game_cycle_span = None
        game._close_grouping_spans()
        mock_span.end.assert_called_once()
        assert game._players_span is None

    def test_closes_game_cycle_span(self, game):
        """Should end and clear _game_cycle_span and game_cycle_context."""
        mock_span = MagicMock()
        game._game_cycle_span = mock_span
        game.game_cycle_context = MagicMock()
        game._players_span = None
        game._close_grouping_spans()
        mock_span.end.assert_called_once()
        assert game._game_cycle_span is None
        assert game.game_cycle_context is None

    def test_handles_none_spans(self, game):
        """Should not crash when spans are already None."""
        game._players_span = None
        game._game_cycle_span = None
        game._close_grouping_spans()  # Should not raise


# ========================================================================
# _record_player_analytics (enabled check)
# ========================================================================


class TestRecordPlayerAnalytics:
    """Tests for _record_player_analytics enabled-check behavior."""

    @pytest.fixture
    def game(self):
        """Create game for analytics tests."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        game = FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_analytics",
        )
        game.music_speed = SLOW_MUSIC_SPEED
        return game

    def test_skips_when_analytics_none(self, game):
        """Should return early when player.analytics is None."""
        player = Player(serial="test", analytics=None)
        accel = controller_manager_pb2.Vector3(x=1.0, y=0.0, z=0.0)
        controller_state = controller_manager_pb2.GameplayData(serial="test", accel=accel)
        # Should not raise
        game._record_player_analytics(player, "test", accel, 1.0, 1.0, 2.0, controller_state)

    def test_skips_when_analytics_disabled(self, game):
        """Should return early when analytics config is disabled."""
        mock_analytics = MagicMock()
        player = Player(serial="test", analytics=mock_analytics)
        accel = controller_manager_pb2.Vector3(x=1.0, y=0.0, z=0.0)
        controller_state = controller_manager_pb2.GameplayData(serial="test", accel=accel)

        # The method checks config.analytics.enabled internally via get_config_manager
        # With disabled metrics in tests, analytics is disabled by default
        game._record_player_analytics(player, "test", accel, 1.0, 1.0, 2.0, controller_state)
        # If analytics was disabled, record_sample should not have been called
        # (This depends on runtime_config default - the test verifies no crash)
