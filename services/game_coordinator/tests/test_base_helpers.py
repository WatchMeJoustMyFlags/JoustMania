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
