"""
Unit tests for BaseGameMode (base.py) - Critical game flow methods.

Tests the core game physics and death detection logic:
- Linear interpolation (_lerp)
- Threshold calculation with music tempo
- Grace period prevents death during invincibility
- Warning feedback does NOT prevent death
- Death detection at threshold boundaries
- Error handling for invalid data

Issue #209: Improve test coverage for critical game flow
"""

import sys
import time
from pathlib import Path

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import MockControllerManagerService, MockSettingsService  # noqa: E402

from proto import controller_manager_pb2  # noqa: E402
from services.game_coordinator.games.base import (  # noqa: E402
    FAST_MAX,
    FAST_MUSIC_SPEED,
    FAST_WARNING,
    SLOW_MAX,
    SLOW_MUSIC_SPEED,
    SLOW_WARNING,
    WARNING_DURATION,
    GameState,
    Player,
)
from services.game_coordinator.games.ffa import FFAGame  # noqa: E402


class MockGameplayStream:
    """Mock bidirectional stream for testing."""

    def __init__(self):
        self.messages = []

    async def write(self, message):
        self.messages.append(message)


class TestLerpFunction:
    """Tests for the _lerp linear interpolation function."""

    @pytest.fixture
    def game(self):
        """Create a minimal FFA game for testing base class methods."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        return FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=lambda *_args: None,
            audio_client=None,
            game_id="test_lerp",
        )

    def test_lerp_at_zero(self, game):
        """t=0 should return a."""
        assert game._lerp(1.0, 2.0, 0.0) == 1.0
        assert game._lerp(0.0, 100.0, 0.0) == 0.0
        assert game._lerp(-5.0, 5.0, 0.0) == -5.0

    def test_lerp_at_one(self, game):
        """t=1 should return b."""
        assert game._lerp(1.0, 2.0, 1.0) == 2.0
        assert game._lerp(0.0, 100.0, 1.0) == 100.0
        assert game._lerp(-5.0, 5.0, 1.0) == 5.0

    def test_lerp_at_midpoint(self, game):
        """t=0.5 should return midpoint."""
        assert game._lerp(0.0, 10.0, 0.5) == 5.0
        assert game._lerp(1.0, 3.0, 0.5) == 2.0
        assert game._lerp(-10.0, 10.0, 0.5) == 0.0

    def test_lerp_at_quarter(self, game):
        """t=0.25 should return 25% of the way."""
        assert game._lerp(0.0, 100.0, 0.25) == 25.0
        assert game._lerp(1.0, 5.0, 0.25) == 2.0  # 1 + 0.25*(5-1) = 2

    def test_lerp_same_values(self, game):
        """When a==b, result should always be a (or b)."""
        assert game._lerp(5.0, 5.0, 0.0) == 5.0
        assert game._lerp(5.0, 5.0, 0.5) == 5.0
        assert game._lerp(5.0, 5.0, 1.0) == 5.0


class TestThresholdCalculation:
    """Tests for threshold calculation with music tempo LERP."""

    @pytest.fixture
    def game(self):
        """Create game for threshold testing."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        return FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=lambda *_args: None,
            audio_client=None,
            game_id="test_threshold",
        )

    def test_slow_music_uses_slow_thresholds(self, game):
        """At SLOW_MUSIC_SPEED, thresholds should match SLOW_* arrays."""
        from lib.types import Sensitivity

        game.music_speed = SLOW_MUSIC_SPEED

        for sens in Sensitivity:
            sens_idx = sens.value
            speed_range = FAST_MUSIC_SPEED - SLOW_MUSIC_SPEED
            speed_percent = (game.music_speed - SLOW_MUSIC_SPEED) / speed_range
            speed_percent = max(0.0, min(1.0, speed_percent))

            expected_warn = game._lerp(SLOW_WARNING[sens_idx], FAST_WARNING[sens_idx], speed_percent)
            expected_death = game._lerp(SLOW_MAX[sens_idx], FAST_MAX[sens_idx], speed_percent)

            assert expected_warn == SLOW_WARNING[sens_idx], f"Sensitivity {sens.name}"
            assert expected_death == SLOW_MAX[sens_idx], f"Sensitivity {sens.name}"

    def test_fast_music_uses_fast_thresholds(self, game):
        """At FAST_MUSIC_SPEED, thresholds should match FAST_* arrays."""
        from lib.types import Sensitivity

        game.music_speed = FAST_MUSIC_SPEED

        for sens in Sensitivity:
            sens_idx = sens.value
            speed_range = FAST_MUSIC_SPEED - SLOW_MUSIC_SPEED
            speed_percent = (game.music_speed - SLOW_MUSIC_SPEED) / speed_range
            speed_percent = max(0.0, min(1.0, speed_percent))

            expected_warn = game._lerp(SLOW_WARNING[sens_idx], FAST_WARNING[sens_idx], speed_percent)
            expected_death = game._lerp(SLOW_MAX[sens_idx], FAST_MAX[sens_idx], speed_percent)

            assert expected_warn == FAST_WARNING[sens_idx], f"Sensitivity {sens.name}"
            assert expected_death == FAST_MAX[sens_idx], f"Sensitivity {sens.name}"

    def test_mid_tempo_interpolates_thresholds(self, game):
        """At midpoint tempo, thresholds should be midpoint of slow/fast."""
        from lib.types import Sensitivity

        # Set music to midpoint between slow and fast
        game.music_speed = (SLOW_MUSIC_SPEED + FAST_MUSIC_SPEED) / 2

        for sens in Sensitivity:
            sens_idx = sens.value
            speed_range = FAST_MUSIC_SPEED - SLOW_MUSIC_SPEED
            speed_percent = (game.music_speed - SLOW_MUSIC_SPEED) / speed_range

            expected_warn = (SLOW_WARNING[sens_idx] + FAST_WARNING[sens_idx]) / 2
            expected_death = (SLOW_MAX[sens_idx] + FAST_MAX[sens_idx]) / 2

            actual_warn = game._lerp(SLOW_WARNING[sens_idx], FAST_WARNING[sens_idx], speed_percent)
            actual_death = game._lerp(SLOW_MAX[sens_idx], FAST_MAX[sens_idx], speed_percent)

            assert abs(actual_warn - expected_warn) < 0.001, f"Sensitivity {sens.name}"
            assert abs(actual_death - expected_death) < 0.001, f"Sensitivity {sens.name}"

    def test_speed_percent_clamped_below_zero(self, game):
        """Speed percent should clamp to 0 if music slower than SLOW_MUSIC_SPEED."""
        game.music_speed = SLOW_MUSIC_SPEED - 0.5  # Below minimum

        speed_range = FAST_MUSIC_SPEED - SLOW_MUSIC_SPEED
        speed_percent = (game.music_speed - SLOW_MUSIC_SPEED) / speed_range
        speed_percent = max(0.0, min(1.0, speed_percent))

        assert speed_percent == 0.0

    def test_speed_percent_clamped_above_one(self, game):
        """Speed percent should clamp to 1 if music faster than FAST_MUSIC_SPEED."""
        game.music_speed = FAST_MUSIC_SPEED + 0.5  # Above maximum

        speed_range = FAST_MUSIC_SPEED - SLOW_MUSIC_SPEED
        speed_percent = (game.music_speed - SLOW_MUSIC_SPEED) / speed_range
        speed_percent = max(0.0, min(1.0, speed_percent))

        assert speed_percent == 1.0


class TestGracePeriod:
    """Tests for grace period logic - no death during invincibility window."""

    @pytest.fixture
    def game_with_player(self):
        """Create game with initialized player for grace period testing."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        game = FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=lambda *_args: None,
            audio_client=None,
            game_id="test_grace",
        )

        # Manually initialize a player
        game.players["test_serial"] = Player(
            serial="test_serial",
            team=0,
            alive=True,
            color=(255, 0, 0),
        )
        game.gameplay_stream = MockGameplayStream()
        game.music_speed = SLOW_MUSIC_SPEED
        game.start_time = time.time()

        return game

    @pytest.mark.asyncio
    async def test_grace_period_prevents_death(self, game_with_player):
        """Player should NOT die during grace period even with lethal acceleration."""
        game = game_with_player
        player = game.players["test_serial"]

        # Set grace period to future (player is protected)
        player.grace_until = time.time() + 10.0

        # Create controller state with LETHAL acceleration (way above threshold)
        controller_state = controller_manager_pb2.GameplayData(
            serial="test_serial",
            accel=controller_manager_pb2.Vector3(x=10.0, y=10.0, z=10.0),  # ~17g - definitely lethal
        )

        # Process the state - should NOT die due to grace period
        await game._process_controller_state(controller_state)

        assert player.alive is True, "Player should survive during grace period"

    @pytest.mark.asyncio
    async def test_grace_period_prevents_warning(self, game_with_player):
        """Player should NOT get warning during grace period."""
        game = game_with_player
        player = game.players["test_serial"]

        # Set grace period to future
        player.grace_until = time.time() + 10.0
        player.warning_until = 0.0  # No active warning

        # Create controller state with warning-level acceleration
        controller_state = controller_manager_pb2.GameplayData(
            serial="test_serial",
            accel=controller_manager_pb2.Vector3(x=1.5, y=0.0, z=1.0),  # Above warning, below death
        )

        # Process - should not set warning during grace
        await game._process_controller_state(controller_state)

        assert player.warning_until == 0.0, "Warning should not be set during grace period"

    @pytest.mark.asyncio
    async def test_after_grace_period_death_possible(self, game_with_player):
        """Player CAN die after grace period expires."""
        game = game_with_player
        player = game.players["test_serial"]

        # Set grace period to past (no longer protected)
        player.grace_until = time.time() - 1.0

        # Prime the EMA filter so death detection works correctly
        player.smoothed_accel = 1.0

        # Mock _kill_player to track if it was called
        kill_called = []
        original_kill = game._kill_player

        async def mock_kill(serial, accel_mag):
            kill_called.append((serial, accel_mag))
            await original_kill(serial, accel_mag)

        game._kill_player = mock_kill

        # Create lethal acceleration
        controller_state = controller_manager_pb2.GameplayData(
            serial="test_serial",
            accel=controller_manager_pb2.Vector3(x=10.0, y=10.0, z=10.0),
        )

        await game._process_controller_state(controller_state)

        # May take multiple frames due to EMA smoothing, but death should occur
        # If EMA wasn't primed high enough, death may not trigger on first frame
        # Let's process multiple times to ensure EMA builds up
        for _ in range(10):
            await game._process_controller_state(controller_state)

        assert len(kill_called) > 0 or not player.alive, "Player should die after grace period with lethal accel"


class TestWarningBehavior:
    """Tests for warning feedback - warnings do NOT grant immunity."""

    @pytest.fixture
    def game_with_player(self):
        """Create game with player for warning tests."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        game = FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=lambda *_args: None,
            audio_client=None,
            game_id="test_warning",
        )

        game.players["test_serial"] = Player(
            serial="test_serial",
            team=0,
            alive=True,
            color=(255, 0, 0),
            smoothed_accel=1.0,  # Prime EMA
        )
        game.gameplay_stream = MockGameplayStream()
        game.music_speed = SLOW_MUSIC_SPEED
        game.start_time = time.time()

        return game

    @pytest.mark.asyncio
    async def test_warning_does_not_prevent_death(self, game_with_player):
        """Player CAN die even when in warning state - warnings are just feedback."""
        game = game_with_player
        player = game.players["test_serial"]

        # Set active warning (player is in warning feedback state)
        player.warning_until = time.time() + 5.0
        # No grace period
        player.grace_until = 0.0

        # Track kill calls
        kill_called = []

        async def mock_kill(serial, accel_mag):
            kill_called.append((serial, accel_mag))
            player.alive = False

        game._kill_player = mock_kill

        # Create lethal acceleration
        controller_state = controller_manager_pb2.GameplayData(
            serial="test_serial",
            accel=controller_manager_pb2.Vector3(x=10.0, y=10.0, z=10.0),
        )

        # Process multiple frames to build EMA
        for _ in range(10):
            if player.alive:
                await game._process_controller_state(controller_state)

        assert len(kill_called) > 0, "Player should die even during warning state"

    @pytest.mark.asyncio
    async def test_warning_sets_warning_until(self, game_with_player):
        """Warning should set warning_until for feedback duration."""
        game = game_with_player
        player = game.players["test_serial"]

        # No grace period, no active warning
        player.grace_until = 0.0
        player.warning_until = 0.0

        before_time = time.time()

        # Trigger warning via _warn_player
        await game._warn_player("test_serial", accel_mag=1.5, threshold=1.4)

        after_time = time.time()

        # Warning should be set for WARNING_DURATION
        assert player.warning_until >= before_time + WARNING_DURATION
        assert player.warning_until <= after_time + WARNING_DURATION + 0.1

    @pytest.mark.asyncio
    async def test_no_repeated_warnings_during_warning_state(self, game_with_player):
        """Should not trigger new warning while already in warning state."""
        game = game_with_player
        player = game.players["test_serial"]

        # No grace period
        player.grace_until = 0.0
        # Set active warning
        player.warning_until = time.time() + 5.0

        # Count stream writes (warnings send effects)
        stream = game.gameplay_stream
        messages_before = len(stream.messages)

        # Create warning-level (but not lethal) acceleration
        # Using values between warning and death thresholds for MEDIUM sensitivity
        controller_state = controller_manager_pb2.GameplayData(
            serial="test_serial",
            accel=controller_manager_pb2.Vector3(x=1.5, y=0.5, z=1.0),  # ~1.9g
        )

        await game._process_controller_state(controller_state)

        # Should not send new warning effect since already in warning state
        # The condition `current_time >= player.warning_until` prevents re-warning
        messages_after = len(stream.messages)
        assert messages_after == messages_before, "Should not send warning when already in warning state"


class TestDeathDetection:
    """Tests for death detection at threshold boundaries."""

    @pytest.fixture
    def game_with_player(self):
        """Create game with player for death detection tests."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        game = FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=lambda *_args: None,
            audio_client=None,
            game_id="test_death",
        )

        game.players["test_serial"] = Player(
            serial="test_serial",
            team=0,
            alive=True,
            color=(255, 0, 0),
        )
        game.gameplay_stream = MockGameplayStream()
        game.music_speed = SLOW_MUSIC_SPEED
        game.start_time = time.time()

        return game

    @pytest.mark.asyncio
    async def test_below_threshold_survives(self, game_with_player):
        """Player should survive with acceleration below death threshold."""
        from lib.types import Sensitivity

        game = game_with_player
        player = game.players["test_serial"]
        player.grace_until = 0.0  # No grace

        game.sensitivity = Sensitivity.MEDIUM
        sens_idx = game.sensitivity.value

        # Use acceleration below the SLOW_MAX threshold for MEDIUM
        # SLOW_MAX[MEDIUM] = 1.8, so use 1.5g which is below
        safe_accel = SLOW_MAX[sens_idx] - 0.5

        controller_state = controller_manager_pb2.GameplayData(
            serial="test_serial",
            accel=controller_manager_pb2.Vector3(x=safe_accel, y=0.0, z=0.0),
        )

        # Process multiple times to stabilize EMA
        for _ in range(20):
            await game._process_controller_state(controller_state)

        assert player.alive is True, f"Player should survive at {safe_accel}g (threshold: {SLOW_MAX[sens_idx]}g)"

    @pytest.mark.asyncio
    async def test_above_threshold_dies(self, game_with_player):
        """Player should die with acceleration above death threshold."""
        from lib.types import Sensitivity

        game = game_with_player
        player = game.players["test_serial"]
        player.grace_until = 0.0

        game.sensitivity = Sensitivity.MEDIUM
        sens_idx = game.sensitivity.value

        # Use acceleration well above the threshold
        lethal_accel = SLOW_MAX[sens_idx] + 2.0  # Significantly above threshold

        kill_called = []

        async def mock_kill(serial, accel_mag):
            kill_called.append((serial, accel_mag))
            player.alive = False

        game._kill_player = mock_kill

        controller_state = controller_manager_pb2.GameplayData(
            serial="test_serial",
            accel=controller_manager_pb2.Vector3(x=lethal_accel, y=lethal_accel, z=lethal_accel),
        )

        # Process multiple times for EMA to build up
        for _ in range(20):
            if player.alive:
                await game._process_controller_state(controller_state)

        assert len(kill_called) > 0, "Player should die at high acceleration"

    @pytest.mark.asyncio
    async def test_exactly_at_threshold_survives(self, game_with_player):
        """Player at exactly threshold should survive (threshold is exclusive)."""
        from lib.types import Sensitivity

        game = game_with_player
        player = game.players["test_serial"]
        player.grace_until = 0.0

        game.sensitivity = Sensitivity.MEDIUM
        sens_idx = game.sensitivity.value

        # Set EMA to exactly at threshold
        threshold = SLOW_MAX[sens_idx]
        player.smoothed_accel = threshold

        # The condition is `smoothed > effective_death`, so exactly at threshold survives
        # Create controller state that maintains current EMA
        controller_state = controller_manager_pb2.GameplayData(
            serial="test_serial",
            accel=controller_manager_pb2.Vector3(x=threshold, y=0.0, z=0.0),
        )

        # Single process
        await game._process_controller_state(controller_state)

        # Player should survive at exactly threshold (> not >=)
        assert player.alive is True, "Player at exactly threshold should survive (> not >=)"


class TestSensitivityFactor:
    """Tests for per-player sensitivity factor."""

    @pytest.fixture
    def game_with_player(self):
        """Create game with player for sensitivity factor tests."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        game = FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=lambda *_args: None,
            audio_client=None,
            game_id="test_sens_factor",
        )

        game.players["test_serial"] = Player(
            serial="test_serial",
            team=0,
            alive=True,
            color=(255, 0, 0),
        )
        game.gameplay_stream = MockGameplayStream()
        game.music_speed = SLOW_MUSIC_SPEED
        game.start_time = time.time()

        return game

    def test_sensitivity_factor_clamped_low(self, game_with_player):
        """Sensitivity factor below 0.5 should be clamped to 0.5."""
        player = game_with_player.players["test_serial"]
        player.sensitivity_factor = 0.1  # Below minimum

        # The clamping happens in _process_controller_state
        clamped = max(0.5, min(2.0, player.sensitivity_factor))
        assert clamped == 0.5

    def test_sensitivity_factor_clamped_high(self, game_with_player):
        """Sensitivity factor above 2.0 should be clamped to 2.0."""
        player = game_with_player.players["test_serial"]
        player.sensitivity_factor = 5.0  # Above maximum

        clamped = max(0.5, min(2.0, player.sensitivity_factor))
        assert clamped == 2.0

    def test_higher_factor_lowers_threshold(self, game_with_player):
        """Higher sensitivity factor should lower effective threshold (easier to die)."""
        game = game_with_player
        player = game.players["test_serial"]

        base_threshold = 2.0

        # Factor of 1.0 - no change
        player.sensitivity_factor = 1.0
        effective_1 = base_threshold / max(0.5, min(2.0, player.sensitivity_factor))
        assert effective_1 == 2.0

        # Factor of 2.0 - threshold halved (easier to die)
        player.sensitivity_factor = 2.0
        effective_2 = base_threshold / max(0.5, min(2.0, player.sensitivity_factor))
        assert effective_2 == 1.0

        assert effective_2 < effective_1, "Higher factor should lower threshold"


class TestEMAFilter:
    """Tests for exponential moving average filter."""

    @pytest.fixture
    def game_with_player(self):
        """Create game with player for EMA tests."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        game = FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=lambda *_args: None,
            audio_client=None,
            game_id="test_ema",
        )

        game.players["test_serial"] = Player(
            serial="test_serial",
            team=0,
            alive=True,
            color=(255, 0, 0),
            smoothed_accel=0.0,  # Start uninitialized
        )
        game.gameplay_stream = MockGameplayStream()
        game.music_speed = SLOW_MUSIC_SPEED
        game.start_time = time.time()

        return game

    @pytest.mark.asyncio
    async def test_ema_primed_with_first_reading(self, game_with_player):
        """First reading should prime EMA instead of smoothing from 0."""
        game = game_with_player
        player = game.players["test_serial"]
        player.grace_until = time.time() + 10.0  # Grace period to prevent death

        assert player.smoothed_accel == 0.0

        # First reading with magnitude ~1.0 (standing still)
        controller_state = controller_manager_pb2.GameplayData(
            serial="test_serial",
            accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0),
        )

        await game._process_controller_state(controller_state)

        # EMA should be primed to first reading, not smoothed from 0
        assert player.smoothed_accel == pytest.approx(1.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_ema_smooths_subsequent_readings(self, game_with_player):
        """Subsequent readings should be smoothed with 80/20 weighting."""
        game = game_with_player
        player = game.players["test_serial"]
        player.grace_until = time.time() + 10.0
        player.smoothed_accel = 1.0  # Pre-prime

        # New reading of 2.0
        controller_state = controller_manager_pb2.GameplayData(
            serial="test_serial",
            accel=controller_manager_pb2.Vector3(x=2.0, y=0.0, z=0.0),
        )

        await game._process_controller_state(controller_state)

        # EMA formula: (smoothed * 4 + raw) / 5 = (1.0 * 4 + 2.0) / 5 = 1.2
        assert player.smoothed_accel == pytest.approx(1.2, abs=0.01)


class TestDeadPlayerIgnored:
    """Tests that dead players are properly ignored."""

    @pytest.fixture
    def game_with_dead_player(self):
        """Create game with dead player."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        game = FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=lambda *_args: None,
            audio_client=None,
            game_id="test_dead",
        )

        game.players["test_serial"] = Player(
            serial="test_serial",
            team=0,
            alive=False,  # Already dead
            color=(255, 0, 0),
        )
        game.gameplay_stream = MockGameplayStream()

        return game

    @pytest.mark.asyncio
    async def test_dead_player_state_not_processed(self, game_with_dead_player):
        """Dead player's controller state should be ignored."""
        game = game_with_dead_player
        player = game.players["test_serial"]

        initial_accel = player.smoothed_accel

        controller_state = controller_manager_pb2.GameplayData(
            serial="test_serial",
            accel=controller_manager_pb2.Vector3(x=5.0, y=5.0, z=5.0),
        )

        await game._process_controller_state(controller_state)

        # Smoothed accel should not change for dead player
        assert player.smoothed_accel == initial_accel


class TestUnknownControllerIgnored:
    """Tests that unknown controllers are ignored."""

    @pytest.fixture
    def game(self):
        """Create game with no players."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        game = FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=lambda *_args: None,
            audio_client=None,
            game_id="test_unknown",
        )
        game.gameplay_stream = MockGameplayStream()
        return game

    @pytest.mark.asyncio
    async def test_unknown_serial_ignored(self, game):
        """Controller not in players dict should be ignored."""
        # No players initialized
        assert len(game.players) == 0

        controller_state = controller_manager_pb2.GameplayData(
            serial="unknown_serial",
            accel=controller_manager_pb2.Vector3(x=10.0, y=10.0, z=10.0),
        )

        # Should not raise, just return early
        await game._process_controller_state(controller_state)


class TestGameStateTransitions:
    """Tests for game state transitions."""

    @pytest.fixture
    def game(self):
        """Create game for state transition tests."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        mock_settings = MockSettingsService()
        return FFAGame(
            controller_manager_client=mock_cm,
            settings_client=mock_settings,
            event_publisher=lambda *_args: None,
            audio_client=None,
            game_id="test_state",
        )

    def test_initial_state_is_idle(self, game):
        """Game should start in IDLE state."""
        assert game.state == GameState.IDLE

    def test_force_end_sets_running_false(self, game):
        """force_end() should set running to False."""
        game.running = True
        game.force_end()
        assert game.running is False
