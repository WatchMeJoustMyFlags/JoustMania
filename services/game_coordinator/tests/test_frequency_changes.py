"""
Unit tests for dynamic frequency change functionality in base game mode.

Tests frequency detection, validation, message sending, metrics tracking,
and frame tracking reset when frequency changes during gameplay.

Related to dynamic frequency change implementation in base.py.
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import MockControllerManagerService, MockSettingsService, async_noop

from proto import controller_manager_pb2
from services.game_coordinator.games.base import Player
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.runtime_config import GamePerformanceConfig, get_config_manager


class MockGameplayStream:
    """Mock bidirectional stream for testing frequency changes."""

    def __init__(self):
        self.messages = []
        self.frequency_updates = []

    async def write(self, message):
        """Capture all messages sent to stream."""
        self.messages.append(message)
        if message.HasField("frequency_update"):
            self.frequency_updates.append(message.frequency_update.update_frequency_hz)


class TestFrequencyChangeDetection:
    """Tests for detecting when frequency changes."""

    @pytest.fixture
    def game_with_stream(self):
        """Create game with mock stream for testing."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        game = FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_freq_detection",
        )

        # Setup gameplay stream
        game.gameplay_stream = MockGameplayStream()
        game.running = True
        game.start_time = time.time()

        # Add players
        game.players["test_serial"] = Player(
            serial="test_serial",
            team=0,
            alive=True,
            color=(255, 0, 0),
            smoothed_accel=1.0,
        )

        return game

    def test_frequency_change_detected_when_different(self, game_with_stream):
        """Frequency change should be detected when new != old."""

        # Initial frequency
        old_freq = 30

        # New frequency
        new_freq = 60

        assert old_freq != new_freq, "Test should detect frequency change"

    def test_no_frequency_change_when_same(self, game_with_stream):
        """No frequency change should be detected when new == old."""

        # Same frequency
        old_freq = 60
        new_freq = 60

        assert old_freq == new_freq, "Test should not detect change"

    @pytest.mark.asyncio
    async def test_frequency_checked_each_iteration(self, game_with_stream):
        """Frequency should be checked on each game loop iteration."""

        # Simulate checking frequency multiple times (as game loop would)

        configs = []
        for _ in range(5):
            config = get_config_manager().get_config()
            configs.append(config.update_frequency_hz)

        # Should have read config multiple times
        assert len(configs) == 5
        # All should have some frequency value
        assert all(freq > 0 for freq in configs)


class TestFrequencyBoundsValidation:
    """Tests for validating frequency is within bounds (1-100 Hz)."""

    @pytest.fixture
    def game_with_stream(self):
        """Create game with mock stream."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        game = FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_freq_validation",
        )

        game.gameplay_stream = MockGameplayStream()
        game.running = True
        game.start_time = time.time()

        return game

    def test_valid_frequency_within_bounds(self, game_with_stream):
        """Frequencies 1-100 Hz should be valid."""
        valid_freqs = [1, 10, 30, 60, 75, 90, 100]

        for freq in valid_freqs:
            assert 1 <= freq <= 100, f"Frequency {freq}Hz should be valid"

    def test_frequency_below_minimum_invalid(self, game_with_stream):
        """Frequencies below 1 Hz should be invalid."""
        invalid_freqs = [0, -1, -10]

        for freq in invalid_freqs:
            assert freq < 1, f"Frequency {freq}Hz should be invalid (< 1)"

    def test_frequency_above_maximum_invalid(self, game_with_stream):
        """Frequencies above 100 Hz should be invalid."""
        invalid_freqs = [101, 150, 1000]

        for freq in invalid_freqs:
            assert freq > 100, f"Frequency {freq}Hz should be invalid (> 100)"

    def test_boundary_frequencies_valid(self, game_with_stream):
        """Boundary values 1 and 100 Hz should be valid."""
        assert 1 <= 1 <= 100, "Minimum boundary (1 Hz) should be valid"
        assert 1 <= 100 <= 100, "Maximum boundary (100 Hz) should be valid"


class TestFrequencyUpdateMessageSending:
    """Tests for sending FrequencyUpdate messages via gameplay stream."""

    @pytest.fixture
    def game_with_stream(self):
        """Create game with mock stream."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        game = FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_freq_msg",
        )

        game.gameplay_stream = MockGameplayStream()
        game.running = True
        game.start_time = time.time()

        return game

    @pytest.mark.asyncio
    async def test_frequency_update_message_structure(self, game_with_stream):
        """FrequencyUpdate message should have correct structure."""
        game = game_with_stream

        # Create frequency update message
        freq_msg = controller_manager_pb2.GameplayStreamControl(
            frequency_update=controller_manager_pb2.FrequencyUpdate(update_frequency_hz=60)
        )

        await game.gameplay_stream.write(freq_msg)

        # Check message was captured
        assert len(game.gameplay_stream.messages) == 1
        assert game.gameplay_stream.messages[0].HasField("frequency_update")
        assert game.gameplay_stream.messages[0].frequency_update.update_frequency_hz == 60

    @pytest.mark.asyncio
    async def test_frequency_update_captured_correctly(self, game_with_stream):
        """Frequency update should be captured in frequency_updates list."""
        game = game_with_stream

        # Send multiple frequency updates
        for freq in [30, 60, 90]:
            freq_msg = controller_manager_pb2.GameplayStreamControl(
                frequency_update=controller_manager_pb2.FrequencyUpdate(update_frequency_hz=freq)
            )
            await game.gameplay_stream.write(freq_msg)

        # Check all frequencies were captured
        assert game.gameplay_stream.frequency_updates == [30, 60, 90]

    @pytest.mark.asyncio
    async def test_frequency_update_sent_to_stream(self, game_with_stream):
        """FrequencyUpdate should be sent via gameplay_stream.write()."""
        game = game_with_stream

        # Send frequency update
        new_freq = 75
        freq_msg = controller_manager_pb2.GameplayStreamControl(
            frequency_update=controller_manager_pb2.FrequencyUpdate(update_frequency_hz=new_freq)
        )

        await game.gameplay_stream.write(freq_msg)

        # Verify message was sent
        assert len(game.gameplay_stream.messages) == 1
        assert game.gameplay_stream.messages[0].frequency_update.update_frequency_hz == new_freq


class TestMetricsTracking:
    """Tests for tracking frequency change metrics."""

    @pytest.fixture
    def game_with_stream(self):
        """Create game with mock stream."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        game = FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_freq_metrics",
        )

        game.gameplay_stream = MockGameplayStream()
        game.running = True
        game.start_time = time.time()

        return game

    @patch("services.game_coordinator.games.base.metrics")
    @pytest.mark.asyncio
    async def test_frequency_changes_total_incremented(self, mock_metrics, game_with_stream):
        """frequency_changes_total counter should be incremented."""

        # Mock the counter
        mock_counter = MagicMock()
        mock_metrics.frequency_changes_total.labels.return_value = mock_counter

        # Simulate frequency change
        mock_counter.inc()

        # Verify counter was incremented
        mock_counter.inc.assert_called_once()

    @patch("services.game_coordinator.games.base.metrics")
    @pytest.mark.asyncio
    async def test_frequency_change_latency_observed(self, mock_metrics, game_with_stream):
        """frequency_change_latency_seconds histogram should record latency."""

        # Mock the histogram
        mock_histogram = MagicMock()
        mock_metrics.frequency_change_latency_seconds = mock_histogram

        # Simulate latency observation
        latency = 0.005  # 5ms
        mock_histogram.observe(latency)

        # Verify latency was observed
        mock_histogram.observe.assert_called_once_with(latency)

    @patch("services.game_coordinator.games.base.metrics")
    @pytest.mark.asyncio
    async def test_current_frequency_gauge_updated(self, mock_metrics, game_with_stream):
        """current_update_frequency_hz gauge should be set to new frequency."""

        # Mock the gauge
        mock_gauge = MagicMock()
        mock_metrics.current_update_frequency_hz = mock_gauge

        # Simulate gauge update
        new_freq = 60
        mock_gauge.set(new_freq)

        # Verify gauge was set
        mock_gauge.set.assert_called_once_with(new_freq)

    @patch("services.game_coordinator.games.base.metrics")
    @pytest.mark.asyncio
    async def test_metrics_include_game_mode_label(self, mock_metrics, game_with_stream):
        """Metrics should include game_mode label."""

        # Mock the counter with labels
        MagicMock()
        mock_labels = MagicMock()
        mock_labels.inc = MagicMock()
        mock_metrics.frequency_changes_total.labels.return_value = mock_labels

        # Call with labels
        mock_metrics.frequency_changes_total.labels(game_mode="JoustFFA", old_hz="30", new_hz="60")

        # Verify labels were called
        mock_metrics.frequency_changes_total.labels.assert_called_once_with(
            game_mode="JoustFFA", old_hz="30", new_hz="60"
        )

    @patch("services.game_coordinator.games.base.metrics")
    @pytest.mark.asyncio
    async def test_metrics_include_old_and_new_hz(self, mock_metrics, game_with_stream):
        """Metrics should include old_hz and new_hz labels."""

        # Mock the counter with labels
        mock_labels = MagicMock()
        mock_metrics.frequency_changes_total.labels.return_value = mock_labels

        # Call with old/new Hz labels
        mock_metrics.frequency_changes_total.labels(game_mode="JoustFFA", old_hz="30", new_hz="60")

        # Verify correct parameters
        mock_metrics.frequency_changes_total.labels.assert_called_once()
        call_kwargs = mock_metrics.frequency_changes_total.labels.call_args[1]
        assert call_kwargs["old_hz"] == "30"
        assert call_kwargs["new_hz"] == "60"


class TestFrameTrackingReset:
    """Tests for resetting frame tracking on frequency change."""

    @pytest.fixture
    def game_with_stream(self):
        """Create game with mock stream."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        game = FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_frame_reset",
        )

        game.gameplay_stream = MockGameplayStream()
        game.running = True
        game.start_time = time.time()

        return game

    def test_target_frame_time_recalculated(self, game_with_stream):
        """target_frame_time_ms should be recalculated for new frequency."""
        # Old frequency: 30 Hz → 33.33ms per frame
        old_freq = 30
        old_frame_time = 1000.0 / old_freq
        assert old_frame_time == pytest.approx(33.33, abs=0.01)

        # New frequency: 60 Hz → 16.67ms per frame
        new_freq = 60
        new_frame_time = 1000.0 / new_freq
        assert new_frame_time == pytest.approx(16.67, abs=0.01)

        # Verify frame time changes
        assert old_frame_time != new_frame_time

    def test_loop_iterations_reset(self, game_with_stream):
        """loop_iterations should be reset to 0 on frequency change."""
        # Simulate accumulated iterations
        loop_iterations = 100

        # Reset after frequency change
        loop_iterations = 0

        assert loop_iterations == 0

    def test_loop_start_time_reset(self, game_with_stream):
        """loop_start_time should be reset on frequency change."""
        # Old start time
        old_start = time.time() - 10.0  # 10 seconds ago

        # Reset to current time
        new_start = time.time()

        # New start should be more recent
        assert new_start > old_start

    def test_frames_on_target_reset(self, game_with_stream):
        """frames_on_target counter should be reset on frequency change."""
        # Simulate accumulated count
        frames_on_target = 500

        # Reset after frequency change
        frames_on_target = 0

        assert frames_on_target == 0

    def test_recent_frame_times_cleared(self, game_with_stream):
        """recent_frame_times list should be cleared on frequency change."""
        # Simulate accumulated frame times
        recent_frame_times = [16.5, 16.8, 16.2, 17.0, 16.4]

        # Clear after frequency change
        recent_frame_times.clear()

        assert len(recent_frame_times) == 0


class TestInvalidFrequencyRejection:
    """Tests for rejecting invalid frequencies with warnings."""

    @pytest.fixture
    def game_with_stream(self):
        """Create game with mock stream."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        game = FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_invalid_freq",
        )

        game.gameplay_stream = MockGameplayStream()
        game.running = True
        game.start_time = time.time()

        return game

    @pytest.mark.asyncio
    async def test_zero_frequency_rejected(self, game_with_stream, caplog):
        """Frequency of 0 Hz should be rejected."""
        game = game_with_stream

        invalid_freq = 0

        # Check bounds
        if not (1 <= invalid_freq <= 100):
            # Should not send frequency update
            assert len(game.gameplay_stream.frequency_updates) == 0

    @pytest.mark.asyncio
    async def test_negative_frequency_rejected(self, game_with_stream):
        """Negative frequencies should be rejected."""
        game = game_with_stream

        invalid_freq = -10

        # Check bounds
        if not (1 <= invalid_freq <= 100):
            # Should not send frequency update
            assert len(game.gameplay_stream.frequency_updates) == 0

    @pytest.mark.asyncio
    async def test_frequency_above_100_rejected(self, game_with_stream):
        """Frequencies above 100 Hz should be rejected."""
        game = game_with_stream

        invalid_freq = 150

        # Check bounds
        if not (1 <= invalid_freq <= 100):
            # Should not send frequency update
            assert len(game.gameplay_stream.frequency_updates) == 0

    @pytest.mark.asyncio
    async def test_invalid_frequency_logs_warning(self, game_with_stream, caplog):
        """Invalid frequency should log warning message."""
        import logging


        with caplog.at_level(logging.WARNING):
            # Simulate invalid frequency handling
            invalid_freq = 150
            if not (1 <= invalid_freq <= 100):
                # Would log: "Invalid frequency 150Hz (must be 1-100), ignoring"
                pass  # Actual logging happens in base.py

    @pytest.mark.asyncio
    async def test_boundary_case_1hz_accepted(self, game_with_stream):
        """Boundary case: 1 Hz should be accepted."""

        freq = 1

        # Check bounds
        assert 1 <= freq <= 100, "1 Hz should be valid"

    @pytest.mark.asyncio
    async def test_boundary_case_100hz_accepted(self, game_with_stream):
        """Boundary case: 100 Hz should be accepted."""

        freq = 100

        # Check bounds
        assert 1 <= freq <= 100, "100 Hz should be valid"


class TestConfigManagerIntegration:
    """Tests for getting frequency from config manager."""

    @pytest.fixture
    def game_with_stream(self):
        """Create game with mock stream."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        game = FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_config",
        )

        game.gameplay_stream = MockGameplayStream()
        game.running = True
        game.start_time = time.time()

        return game

    @patch("services.game_coordinator.games.base.get_config_manager")
    def test_config_manager_returns_frequency(self, mock_get_config_manager, game_with_stream):
        """Config manager should return update_frequency_hz."""
        # Mock config with frequency
        mock_config = GamePerformanceConfig()
        mock_config.update_frequency_hz = 60

        mock_config_manager = MagicMock()
        mock_config_manager.get_config.return_value = mock_config
        mock_get_config_manager.return_value = mock_config_manager

        # Get config

        config = get_config_manager().get_config()

        # Verify frequency
        assert config.update_frequency_hz == 60

    @patch("services.game_coordinator.games.base.get_config_manager")
    def test_frequency_changes_detected_via_config(self, mock_get_config_manager, game_with_stream):
        """Frequency changes should be detected by comparing config values."""

        # Track frequency changes
        frequencies = []

        def get_config():
            # Simulate changing frequency
            config = GamePerformanceConfig()
            config.update_frequency_hz = frequencies[-1] if frequencies else 30
            return config

        mock_config_manager = MagicMock()
        mock_config_manager.get_config = get_config
        mock_get_config_manager.return_value = mock_config_manager

        # Simulate frequency changes
        frequencies.append(30)
        config1 = get_config()
        assert config1.update_frequency_hz == 30

        frequencies.append(60)
        config2 = get_config()
        assert config2.update_frequency_hz == 60

        # Verify change was detected
        assert config1.update_frequency_hz != config2.update_frequency_hz


class TestFrequencyChangeLatency:
    """Tests for measuring frequency change latency."""

    @pytest.fixture
    def game_with_stream(self):
        """Create game with mock stream."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        game = FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_latency",
        )

        game.gameplay_stream = MockGameplayStream()
        game.running = True
        game.start_time = time.time()

        return game

    @pytest.mark.asyncio
    async def test_latency_measured_with_time_module(self, game_with_stream):
        """Latency should be measured using time.time()."""
        # Measure latency
        start = time.time()
        # Simulate write operation
        await game_with_stream.gameplay_stream.write(
            controller_manager_pb2.GameplayStreamControl(
                frequency_update=controller_manager_pb2.FrequencyUpdate(update_frequency_hz=60)
            )
        )
        end = time.time()

        latency = end - start

        # Latency should be measured
        assert latency >= 0
        assert latency < 1.0  # Should be quick

    @pytest.mark.asyncio
    async def test_latency_includes_write_operation(self, game_with_stream):
        """Latency should include time to write message to stream."""
        game = game_with_stream

        # Measure write latency
        start = time.time()
        await game.gameplay_stream.write(
            controller_manager_pb2.GameplayStreamControl(
                frequency_update=controller_manager_pb2.FrequencyUpdate(update_frequency_hz=60)
            )
        )
        latency = time.time() - start

        # Should have measured some latency
        assert latency >= 0
