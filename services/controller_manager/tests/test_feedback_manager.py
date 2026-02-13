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
