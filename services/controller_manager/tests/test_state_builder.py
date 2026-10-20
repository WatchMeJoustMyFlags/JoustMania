"""
Unit tests for StateBuilder.

Tests the controller state message building (the per-frame cache was removed in #1675).

Issue #209: Improve test coverage for critical game flow
"""

import sys
from pathlib import Path

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from lib.controller_constants import AxisKey, ButtonKey, ControllerInfoKey, StateKey
from services.controller_manager.state_builder import StateBuilder


class MockMonitoring:
    """Mock controller monitoring."""

    def __init__(self):
        self.last_battery_check = 0.0

    def check_battery_levels(self, tracked):
        pass


class TestStateBuilderInit:
    """Tests for StateBuilder initialization."""

    def test_init_creates_pools(self):
        """StateBuilder pools only Vector3 messages; ControllerState is built fresh (#1708)."""
        cache = StateBuilder(MockMonitoring())
        assert not hasattr(cache, "_controller_state_pool")
        assert cache._vector3_pool is not None

    def test_init_empty_controller_states(self):
        """StateBuilder should start with empty controller_states."""
        cache = StateBuilder(MockMonitoring())
        assert cache._controller_states == {}


class TestStateBuilderSetControllerStates:
    """Tests for set_controller_states method."""

    def test_set_controller_states(self):
        """set_controller_states should store reference."""
        cache = StateBuilder(MockMonitoring())
        states = {"serial_1": {"battery": 80}}

        cache.set_controller_states(states)

        assert cache._controller_states is states


class TestStateBuilderBuildMessage:
    """Tests for _build_controller_state_message method."""

    def test_build_message_with_state(self):
        """_build_controller_state_message should populate all fields."""
        cache = StateBuilder(MockMonitoring())
        cache._controller_states = {
            "serial_1": {
                ButtonKey.TRIGGER: True,
                ButtonKey.MOVE: False,
                ButtonKey.CROSS: True,
                ButtonKey.CIRCLE: False,
                ButtonKey.SQUARE: True,
                ButtonKey.TRIANGLE: False,
                ButtonKey.PS: True,
                ButtonKey.SELECT: False,
                ButtonKey.START: True,
                StateKey.ACCEL: {AxisKey.X: 0.1, AxisKey.Y: 0.2, AxisKey.Z: 1.0},
                StateKey.GYRO: {AxisKey.X: 0.01, AxisKey.Y: 0.02, AxisKey.Z: 0.03},
            }
        }
        info = {
            ControllerInfoKey.BATTERY: 85,
            ControllerInfoKey.TEAM: 2,
            ControllerInfoKey.MOVE_NUM: 3,
            ControllerInfoKey.NAME: "TestController",
        }

        result = cache._build_controller_state_message("serial_1", info)

        assert result.serial == "serial_1"
        assert result.battery == 85
        assert result.team == 2
        assert result.move_num == 3
        assert result.name == "TestController"
        assert result.trigger_pressed is True
        assert result.move_pressed is False
        assert result.cross_pressed is True
        assert result.ps_pressed is True
        assert result.start_pressed is True

    def test_build_message_without_state(self):
        """_build_controller_state_message should handle missing state."""
        cache = StateBuilder(MockMonitoring())
        cache._controller_states = {}
        info = {
            ControllerInfoKey.BATTERY: 80,
            ControllerInfoKey.TEAM: 0,
            ControllerInfoKey.MOVE_NUM: 0,
            ControllerInfoKey.NAME: "",
        }

        result = cache._build_controller_state_message("serial_1", info)

        assert result.serial == "serial_1"
        assert result.battery == 80
        assert result.trigger_pressed is False
        assert result.move_pressed is False


class TestStateBuilderRename:
    """UC-002 A2 step 3: a rename reaches the controller state a game reads."""

    @pytest.mark.uc("UC-002", flow="A2", steps=[3])
    def test_renamed_controller_state_carries_new_name(self):
        """After a rename the state a new game reads has the new name, even for a still controller."""
        cache = StateBuilder(MockMonitoring())
        cache._controller_states = {
            "serial_1": {
                **dict.fromkeys(ButtonKey, False),
                StateKey.ACCEL: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 1.0},
                StateKey.GYRO: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 0.0},
            }
        }
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 0, ControllerInfoKey.NAME: "Old Name"}

        assert cache.build_state("serial_1", info).name == "Old Name"

        info[ControllerInfoKey.NAME] = "New Name"
        assert cache.build_state("serial_1", info).name == "New Name"

    @pytest.mark.uc("UC-002", flow="A2", steps=[3])
    def test_renamed_controller_without_hardware_state_carries_new_name(self):
        """A rename also reaches a controller without hardware state."""
        cache = StateBuilder(MockMonitoring())
        cache._controller_states = {}
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 0, ControllerInfoKey.NAME: "Old Name"}

        assert cache.build_state("serial_1", info).name == "Old Name"

        info[ControllerInfoKey.NAME] = "New Name"
        assert cache.build_state("serial_1", info).name == "New Name"


class TestStateBuilderServesEveryField:
    """Regression (#1638): the built ControllerState matches the live controller.

    A still controller keeps accel/gyro unchanged; a change of SELECT, START or move number
    must still reach the next state. Kept after the cache was removed (#1675) as a guard
    against a new cache. Protective only: today's only caller (the gameplay stream) reads
    none of these fields, and move_num is never set in production.
    """

    @staticmethod
    def _still_controller_state() -> dict:
        return {
            **dict.fromkeys(ButtonKey, False),
            StateKey.ACCEL: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 1.0},
            StateKey.GYRO: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 0.0},
        }

    @pytest.mark.parametrize(
        ("button", "field"),
        [(ButtonKey.SELECT, "select_pressed"), (ButtonKey.START, "start_pressed")],
    )
    def test_button_flip_on_still_controller_is_served(self, button, field):
        cache = StateBuilder(MockMonitoring())
        cache._controller_states = {"serial_1": self._still_controller_state()}
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 0, ControllerInfoKey.MOVE_NUM: 1}

        assert getattr(cache.build_state("serial_1", info), field) is False

        cache._controller_states["serial_1"][button] = True
        assert getattr(cache.build_state("serial_1", info), field) is True

        cache._controller_states["serial_1"][button] = False
        assert getattr(cache.build_state("serial_1", info), field) is False

    def test_move_num_change_on_still_controller_is_served(self):
        cache = StateBuilder(MockMonitoring())
        cache._controller_states = {"serial_1": self._still_controller_state()}
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 0, ControllerInfoKey.MOVE_NUM: 1}

        assert cache.build_state("serial_1", info).move_num == 1

        info[ControllerInfoKey.MOVE_NUM] = 2
        assert cache.build_state("serial_1", info).move_num == 2

    def test_move_num_change_without_hardware_state_is_served(self):
        cache = StateBuilder(MockMonitoring())
        cache._controller_states = {}
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 0, ControllerInfoKey.MOVE_NUM: 1}

        assert cache.build_state("serial_1", info).move_num == 1

        info[ControllerInfoKey.MOVE_NUM] = 2
        assert cache.build_state("serial_1", info).move_num == 2
