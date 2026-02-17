"""
Unit tests for FeedbackManager.

Tests the apply_base_color() method that consolidates the cancel-if-cancellable
+ store-and-apply logic used by both stream RPCs.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from proto import controller_manager_pb2
from services.controller_manager.effects_base import ActiveEffect
from services.controller_manager.feedback_manager import FeedbackManager


class TestApplyBaseColor:
    """Tests for FeedbackManager.apply_base_color()."""

    @pytest.fixture
    def feedback_manager(self):
        """Create FeedbackManager with mock backend."""
        backend = MagicMock()
        backend.set_led_color = AsyncMock(return_value=True)
        backend.set_effect_active = MagicMock()
        tracked = {"AA:BB": {"battery": 80}}
        return FeedbackManager(backend=backend, tracked_controllers=tracked)

    @pytest.mark.asyncio
    async def test_apply_base_color_no_active_effect(self, feedback_manager):
        """apply_base_color should set color immediately when no effect is active."""
        fm = feedback_manager
        fm.set_controller_color = AsyncMock(return_value=True)

        await fm.apply_base_color("AA:BB", (255, 0, 0), label="Test")

        assert fm.base_colors["AA:BB"] == (255, 0, 0)
        fm.set_controller_color.assert_called_once_with("AA:BB", (255, 0, 0))

    @pytest.mark.asyncio
    async def test_apply_base_color_blocked_by_non_cancellable_effect(self, feedback_manager):
        """apply_base_color should store color but not set LED when effect is active."""
        fm = feedback_manager
        fm.set_controller_color = AsyncMock(return_value=True)

        # Add a non-cancellable effect (PLAYER_DEATH is not in cancellable_effects)
        done_future = asyncio.get_running_loop().create_future()
        done_future.set_result(None)
        fm.active_effects["AA:BB"] = ActiveEffect(
            task=done_future,
            effect_type=controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH,
        )

        await fm.apply_base_color("AA:BB", (0, 255, 0), label="Test")

        # Color should be stored but not applied
        assert fm.base_colors["AA:BB"] == (0, 255, 0)
        fm.set_controller_color.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_base_color_cancels_cancellable_effect(self, feedback_manager):
        """apply_base_color should cancel cancellable effects then apply color."""
        fm = feedback_manager
        fm.set_controller_color = AsyncMock(return_value=True)

        # Add a cancellable effect (FORCE_START_CHARGE)
        task = asyncio.create_task(asyncio.sleep(100))
        fm.active_effects["AA:BB"] = ActiveEffect(
            task=task,
            effect_type=controller_manager_pb2.GAME_EFFECT_FORCE_START_CHARGE,
        )

        await fm.apply_base_color("AA:BB", (0, 0, 255), label="Test")

        # Cancellable effect should have been cancelled and removed
        assert "AA:BB" not in fm.active_effects
        assert fm.base_colors["AA:BB"] == (0, 0, 255)
        fm.set_controller_color.assert_called_once_with("AA:BB", (0, 0, 255))

    @pytest.mark.asyncio
    async def test_apply_base_color_overwrites_previous_base_color(self, feedback_manager):
        """apply_base_color should overwrite previously stored base color."""
        fm = feedback_manager
        fm.set_controller_color = AsyncMock(return_value=True)

        await fm.apply_base_color("AA:BB", (255, 0, 0), label="Test")
        await fm.apply_base_color("AA:BB", (0, 255, 0), label="Test")

        assert fm.base_colors["AA:BB"] == (0, 255, 0)
        assert fm.set_controller_color.call_count == 2

    @pytest.mark.asyncio
    async def test_apply_base_color_stores_even_when_blocked(self, feedback_manager):
        """apply_base_color should always store the color, even if LED can't be set."""
        fm = feedback_manager
        fm.set_controller_color = AsyncMock(return_value=True)

        # Add a non-cancellable effect
        done_future = asyncio.get_running_loop().create_future()
        done_future.set_result(None)
        fm.active_effects["AA:BB"] = ActiveEffect(
            task=done_future,
            effect_type=controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW,
        )

        await fm.apply_base_color("AA:BB", (128, 128, 0), label="Test")

        # Color stored for later restore, but LED not set now
        assert fm.base_colors["AA:BB"] == (128, 128, 0)
        fm.set_controller_color.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_base_color_no_effect_on_other_controllers(self, feedback_manager):
        """apply_base_color should not affect other controllers' effects."""
        fm = feedback_manager
        fm.tracked_controllers["CC:DD"] = {"battery": 90}
        fm.set_controller_color = AsyncMock(return_value=True)

        # Add effect on a different controller
        done_future = asyncio.get_running_loop().create_future()
        done_future.set_result(None)
        fm.active_effects["CC:DD"] = ActiveEffect(
            task=done_future,
            effect_type=controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH,
        )

        await fm.apply_base_color("AA:BB", (255, 0, 0), label="Test")

        # AA:BB should get color, CC:DD effect should be untouched
        assert fm.base_colors["AA:BB"] == (255, 0, 0)
        fm.set_controller_color.assert_called_once_with("AA:BB", (255, 0, 0))
        assert "CC:DD" in fm.active_effects

    @pytest.mark.asyncio
    async def test_apply_base_color_default_label(self, feedback_manager):
        """apply_base_color should work with default empty label."""
        fm = feedback_manager
        fm.set_controller_color = AsyncMock(return_value=True)

        # Should not raise with default label
        await fm.apply_base_color("AA:BB", (100, 200, 50))

        assert fm.base_colors["AA:BB"] == (100, 200, 50)


class TestHandleGameEffect:
    """Tests for FeedbackManager.handle_game_effect() dispatch."""

    @pytest.fixture
    def feedback_manager(self):
        """Create FeedbackManager with mock backend and two tracked controllers."""
        backend = MagicMock()
        backend.set_led_color = AsyncMock(return_value=True)
        backend.set_rumble = AsyncMock(return_value=True)
        backend.set_effect_active = MagicMock()
        tracked = {"AA:BB": {"battery": 80}, "CC:DD": {"battery": 60}}
        return FeedbackManager(backend=backend, tracked_controllers=tracked)

    @pytest.mark.asyncio
    async def test_dispatch_player_warning(self, feedback_manager):
        """handle_game_effect dispatches PLAYER_WARNING to _effect_player_warning."""
        fm = feedback_manager
        fm._effect_player_warning = AsyncMock()

        result = await fm.handle_game_effect("AA:BB", controller_manager_pb2.GAME_EFFECT_PLAYER_WARNING)

        assert result is True
        fm._effect_player_warning.assert_called_once()
        call_kwargs = fm._effect_player_warning.call_args[1]
        assert call_kwargs["serial"] == "AA:BB"

    @pytest.mark.asyncio
    async def test_dispatch_player_death(self, feedback_manager):
        """handle_game_effect dispatches PLAYER_DEATH to _effect_player_death."""
        fm = feedback_manager
        fm._effect_player_death = AsyncMock()

        result = await fm.handle_game_effect("AA:BB", controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH)

        assert result is True
        fm._effect_player_death.assert_called_once()
        assert fm._effect_player_death.call_args[1]["serial"] == "AA:BB"

    @pytest.mark.asyncio
    async def test_dispatch_player_respawn(self, feedback_manager):
        """handle_game_effect dispatches PLAYER_RESPAWN to _effect_player_respawn."""
        fm = feedback_manager
        fm._effect_player_respawn = AsyncMock()

        result = await fm.handle_game_effect("AA:BB", controller_manager_pb2.GAME_EFFECT_PLAYER_RESPAWN)

        assert result is True
        fm._effect_player_respawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_rumble(self, feedback_manager):
        """handle_game_effect dispatches RUMBLE with duration and speed kwargs."""
        fm = feedback_manager
        fm._effect_rumble = AsyncMock()

        result = await fm.handle_game_effect(
            "AA:BB",
            controller_manager_pb2.GAME_EFFECT_RUMBLE,
            duration_ms=1500,
            speed=7,
        )

        assert result is True
        call_kwargs = fm._effect_rumble.call_args[1]
        assert call_kwargs["duration_ms"] == 1500
        assert call_kwargs["speed"] == 7

    @pytest.mark.asyncio
    async def test_dispatch_none_clears_tracking(self, feedback_manager):
        """handle_game_effect dispatches GAME_EFFECT_NONE to _effect_none."""
        fm = feedback_manager
        fm._effect_none = AsyncMock()

        result = await fm.handle_game_effect("AA:BB", controller_manager_pb2.GAME_EFFECT_NONE)

        assert result is True
        fm._effect_none.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_serial_returns_false(self, feedback_manager):
        """handle_game_effect returns False for unknown serial."""
        fm = feedback_manager

        result = await fm.handle_game_effect("XX:YY", controller_manager_pb2.GAME_EFFECT_PLAYER_WARNING)

        assert result is False

    @pytest.mark.asyncio
    async def test_broadcast_to_all_controllers(self, feedback_manager):
        """handle_game_effect with empty serial broadcasts to all tracked controllers."""
        fm = feedback_manager
        fm._effect_player_warning = AsyncMock()

        result = await fm.handle_game_effect("", controller_manager_pb2.GAME_EFFECT_PLAYER_WARNING)

        assert result is True
        assert fm._effect_player_warning.call_count == 2
        called_serials = {call[1]["serial"] for call in fm._effect_player_warning.call_args_list}
        assert called_serials == {"AA:BB", "CC:DD"}

    @pytest.mark.asyncio
    async def test_unknown_effect_returns_false(self, feedback_manager):
        """handle_game_effect returns False for unknown effect enum value."""
        fm = feedback_manager

        result = await fm.handle_game_effect("AA:BB", 9999)

        assert result is False

    @pytest.mark.asyncio
    async def test_sets_active_effect_type_before_handler(self, feedback_manager):
        """handle_game_effect sets effect_type in active_effects before calling handler."""
        fm = feedback_manager
        captured_effect_type = None

        async def capturing_handler(**_kwargs):
            # Capture what effect_type is set when the handler is called
            nonlocal captured_effect_type
            if "AA:BB" in fm.active_effects:
                captured_effect_type = fm.active_effects["AA:BB"].effect_type

        fm._effect_player_warning = capturing_handler

        await fm.handle_game_effect("AA:BB", controller_manager_pb2.GAME_EFFECT_PLAYER_WARNING)

        assert captured_effect_type == controller_manager_pb2.GAME_EFFECT_PLAYER_WARNING


class TestCancelIfCancellable:
    """Tests for FeedbackManager.cancel_if_cancellable()."""

    @pytest.fixture
    def feedback_manager(self):
        """Create FeedbackManager with mock backend."""
        backend = MagicMock()
        backend.set_led_color = AsyncMock(return_value=True)
        backend.set_effect_active = MagicMock()
        tracked = {"AA:BB": {"battery": 80}}
        return FeedbackManager(backend=backend, tracked_controllers=tracked)

    @pytest.mark.asyncio
    async def test_cancels_cancellable_effect(self, feedback_manager):
        """cancel_if_cancellable should cancel FORCE_START_CHARGE effect."""
        fm = feedback_manager
        task = asyncio.create_task(asyncio.sleep(100))
        fm.active_effects["AA:BB"] = ActiveEffect(
            task=task,
            effect_type=controller_manager_pb2.GAME_EFFECT_FORCE_START_CHARGE,
        )

        result = await fm.cancel_if_cancellable("AA:BB")

        assert result is True
        assert "AA:BB" not in fm.active_effects

    @pytest.mark.asyncio
    async def test_does_not_cancel_non_cancellable_effect(self, feedback_manager):
        """cancel_if_cancellable should not cancel PLAYER_DEATH effect."""
        fm = feedback_manager
        done_future = asyncio.get_running_loop().create_future()
        done_future.set_result(None)
        fm.active_effects["AA:BB"] = ActiveEffect(
            task=done_future,
            effect_type=controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH,
        )

        result = await fm.cancel_if_cancellable("AA:BB")

        assert result is False
        assert "AA:BB" in fm.active_effects

    @pytest.mark.asyncio
    async def test_returns_false_when_no_effect(self, feedback_manager):
        """cancel_if_cancellable should return False when no effect is active."""
        fm = feedback_manager

        result = await fm.cancel_if_cancellable("AA:BB")

        assert result is False


class TestGetBatteryColor:
    """Tests for FeedbackManager._get_battery_color() pure function."""

    @pytest.fixture
    def feedback_manager(self):
        """Create FeedbackManager with mock backend."""
        backend = MagicMock()
        backend.set_led_color = AsyncMock(return_value=True)
        backend.set_effect_active = MagicMock()
        tracked = {}
        return FeedbackManager(backend=backend, tracked_controllers=tracked)

    def test_full_battery_green(self, feedback_manager):
        """100% battery should return Green."""
        from lib.colors import Colors

        assert feedback_manager._get_battery_color(100) == Colors.Green.value

    def test_80_percent_turquoise(self, feedback_manager):
        """80% battery should return Turquoise."""
        from lib.colors import Colors

        assert feedback_manager._get_battery_color(80) == Colors.Turquoise.value

    def test_60_percent_blue(self, feedback_manager):
        """60% battery should return Blue."""
        from lib.colors import Colors

        assert feedback_manager._get_battery_color(60) == Colors.Blue.value

    def test_40_percent_yellow(self, feedback_manager):
        """40% battery should return Yellow."""
        from lib.colors import Colors

        assert feedback_manager._get_battery_color(40) == Colors.Yellow.value

    def test_low_battery_red(self, feedback_manager):
        """Below 40% battery should return Red."""
        from lib.colors import Colors

        assert feedback_manager._get_battery_color(30) == Colors.Red.value
        assert feedback_manager._get_battery_color(0) == Colors.Red.value

    def test_boundary_values(self, feedback_manager):
        """Battery boundaries: 39% is Red, 59% is Yellow, 79% is Blue, 99% is Turquoise."""
        from lib.colors import Colors

        assert feedback_manager._get_battery_color(39) == Colors.Red.value
        assert feedback_manager._get_battery_color(59) == Colors.Yellow.value
        assert feedback_manager._get_battery_color(79) == Colors.Blue.value
        assert feedback_manager._get_battery_color(99) == Colors.Turquoise.value
        assert feedback_manager._get_battery_color(101) == Colors.Green.value
