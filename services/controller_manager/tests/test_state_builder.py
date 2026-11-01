"""
Unit tests for StateBuilder.

Tests the per-controller frame inputs (the per-frame cache was removed in #1675, the
ControllerState message and the Vector3 pool in #2353).

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
from services.controller_manager.state_builder import FrameInputs, StateBuilder


class MockMonitoring:
    """Mock controller monitoring."""

    def __init__(self):
        self.last_battery_check = 0.0

    def check_battery_levels(self, tracked):
        pass


class TestStateBuilderInit:
    """Tests for StateBuilder initialization."""

    def test_init_holds_no_message_pools(self):
        """StateBuilder builds no messages, so it pools none (#1708, #2353)."""
        cache = StateBuilder(MockMonitoring())
        assert not hasattr(cache, "_controller_state_pool")
        assert not hasattr(cache, "_vector3_pool")

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


class TestStateBuilderFrameInputs:
    """Tests for frame_inputs (#2353)."""

    def test_frame_inputs_with_state(self):
        """frame_inputs reads every GameplayData value from the state, info and LED colour."""
        cache = StateBuilder(MockMonitoring())
        cache._controller_states = {
            "serial_1": {
                ButtonKey.TRIGGER: True,
                ButtonKey.MOVE: False,
                ButtonKey.CROSS: True,
                StateKey.ACCEL: {AxisKey.X: 0.1, AxisKey.Y: 0.2, AxisKey.Z: 1.0},
                StateKey.GYRO: {AxisKey.X: 0.01, AxisKey.Y: 0.02, AxisKey.Z: 0.03},
            }
        }
        cache.set_led_colors({"serial_1": (1, 2, 3)})
        info = {
            ControllerInfoKey.BATTERY: 85,
            ControllerInfoKey.TEAM: 2,
            ControllerInfoKey.MOVE_NUM: 3,
            ControllerInfoKey.NAME: "TestController",
        }

        result = cache.frame_inputs("serial_1", info)

        assert result == FrameInputs(
            serial="serial_1",
            move_num=3,
            battery=85,
            team=2,
            color={"r": 1, "g": 2, "b": 3},
            accel={"x": 0.1, "y": 0.2, "z": 1.0},
            gyro={"x": 0.01, "y": 0.02, "z": 0.03},
            rssi=0,
            name="TestController",
            trigger_pressed=True,
        )

    @pytest.mark.parametrize("state", [None, {}, {ButtonKey.MOVE: True}], ids=["no-sample", "empty", "no-motion-keys"])
    def test_frame_inputs_default_to_zero_vectors_and_released(self, state):
        """A missing sample or motion key reads as zero vectors, unlit LED, trigger released."""
        cache = StateBuilder(MockMonitoring())
        cache._controller_states = {} if state is None else {"serial_1": state}

        result = cache.frame_inputs("serial_1", {})

        assert result == FrameInputs(
            serial="serial_1",
            move_num=0,
            battery=0,
            team=0,
            color={"r": 0, "g": 0, "b": 0},
            accel={"x": 0, "y": 0, "z": 0},
            gyro={"x": 0, "y": 0, "z": 0},
            rssi=0,
            name="",
            trigger_pressed=False,
        )

    def test_frame_inputs_copy_the_sample_and_never_alias_it(self):
        """The poll's sample is read, never shared: changing the result leaves the sample and defaults alone."""
        sample = {
            StateKey.ACCEL: {AxisKey.X: 0.1, AxisKey.Y: 0.2, AxisKey.Z: 1.0},
            StateKey.GYRO: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 0.0},
        }
        cache = StateBuilder(MockMonitoring())
        cache._controller_states = {"serial_1": sample}

        first = cache.frame_inputs("serial_1", {})
        first.accel["x"] = 9.0
        bare = cache.frame_inputs("serial_2", {})
        bare.gyro["x"] = 9.0

        assert first.accel is not sample[StateKey.ACCEL]
        assert sample[StateKey.ACCEL][AxisKey.X] == 0.1
        assert cache.frame_inputs("serial_1", {}).accel["x"] == 0.1
        assert cache.frame_inputs("serial_2", {}).gyro == {"x": 0, "y": 0, "z": 0}


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

        assert cache.frame_inputs("serial_1", info).name == "Old Name"

        info[ControllerInfoKey.NAME] = "New Name"
        assert cache.frame_inputs("serial_1", info).name == "New Name"

    @pytest.mark.uc("UC-002", flow="A2", steps=[3])
    def test_renamed_controller_without_hardware_state_carries_new_name(self):
        """A rename also reaches a controller without hardware state."""
        cache = StateBuilder(MockMonitoring())
        cache._controller_states = {}
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 0, ControllerInfoKey.NAME: "Old Name"}

        assert cache.frame_inputs("serial_1", info).name == "Old Name"

        info[ControllerInfoKey.NAME] = "New Name"
        assert cache.frame_inputs("serial_1", info).name == "New Name"


class TestStateBuilderServesEveryField:
    """Regression (#1638): the frame inputs match the live controller.

    A still controller keeps accel/gyro unchanged; a change of the trigger, a small accel
    change or the move number must still reach the next frame. Kept after the cache was
    removed (#1675) as a guard against a new cache. move_num is never set in production.
    The other buttons are not in the gameplay frame (StreamButtonEvents carries them).
    """

    @staticmethod
    def _still_controller_state() -> dict:
        return {
            **dict.fromkeys(ButtonKey, False),
            StateKey.ACCEL: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 1.0},
            StateKey.GYRO: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 0.0},
        }

    def test_trigger_flip_on_still_controller_is_served(self):
        cache = StateBuilder(MockMonitoring())
        cache._controller_states = {"serial_1": self._still_controller_state()}
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 0, ControllerInfoKey.MOVE_NUM: 1}

        assert cache.frame_inputs("serial_1", info).trigger_pressed is False

        cache._controller_states["serial_1"][ButtonKey.TRIGGER] = True
        assert cache.frame_inputs("serial_1", info).trigger_pressed is True

        cache._controller_states["serial_1"][ButtonKey.TRIGGER] = False
        assert cache.frame_inputs("serial_1", info).trigger_pressed is False

    def test_small_accel_change_on_still_controller_is_served(self):
        cache = StateBuilder(MockMonitoring())
        cache._controller_states = {"serial_1": self._still_controller_state()}

        assert cache.frame_inputs("serial_1", {}).accel["x"] == 0.0

        cache._controller_states["serial_1"][StateKey.ACCEL] = {AxisKey.X: 0.001, AxisKey.Y: 0.0, AxisKey.Z: 1.0}
        assert cache.frame_inputs("serial_1", {}).accel["x"] == 0.001

    def test_move_num_change_on_still_controller_is_served(self):
        cache = StateBuilder(MockMonitoring())
        cache._controller_states = {"serial_1": self._still_controller_state()}
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 0, ControllerInfoKey.MOVE_NUM: 1}

        assert cache.frame_inputs("serial_1", info).move_num == 1

        info[ControllerInfoKey.MOVE_NUM] = 2
        assert cache.frame_inputs("serial_1", info).move_num == 2

    def test_move_num_change_without_hardware_state_is_served(self):
        cache = StateBuilder(MockMonitoring())
        cache._controller_states = {}
        info = {ControllerInfoKey.BATTERY: 80, ControllerInfoKey.TEAM: 0, ControllerInfoKey.MOVE_NUM: 1}

        assert cache.frame_inputs("serial_1", info).move_num == 1

        info[ControllerInfoKey.MOVE_NUM] = 2
        assert cache.frame_inputs("serial_1", info).move_num == 2
