"""
Controller state message building for ControllerManager.

Builds the ControllerState protobuf a gameplay frame reads, from the live
controller state, using pooled messages (Phase 18). The name is historical: the
per-frame state cache was removed in #1675 (it did not pay off).
"""

import logging
from typing import TYPE_CHECKING

from lib.controller_constants import (
    AxisKey,
    ButtonKey,
    ControllerInfoKey,
    StateKey,
)
from proto import controller_manager_pb2
from services.controller_manager.message_pool import MessagePool

if TYPE_CHECKING:
    from services.controller_manager.monitoring import ControllerMonitoring

logger = logging.getLogger(__name__)


class StateCache:
    """Builds ControllerState protobuf messages from the live controller state (no cache, #1675)."""

    def __init__(self, monitoring: "ControllerMonitoring"):
        """
        Initialize the state builder.

        Args:
            monitoring: ControllerMonitoring instance for RSSI data
        """
        self.monitoring = monitoring

        # Protobuf object pools (Phase 18 - Task 3, Issue #542: scaled for 20+ controllers)
        self._controller_state_pool = MessagePool(controller_manager_pb2.ControllerState, pool_size=30)
        self._vector3_pool = MessagePool(controller_manager_pb2.Vector3, pool_size=60)

        # Reference to shared state (set by servicer)
        self._controller_states: dict[str, dict] = {}

        # Reference to the LED colour last written per controller (set by the
        # servicer from FeedbackManager.current_colors; #1301).
        self._led_colors: dict[str, tuple[int, int, int]] = {}

    def set_controller_states(self, controller_states: dict[str, dict]) -> None:
        """Set reference to the shared controller_states dict."""
        self._controller_states = controller_states

    def set_led_colors(self, led_colors: dict[str, tuple[int, int, int]]) -> None:
        """Set reference to the shared current-LED-colour dict (#1301)."""
        self._led_colors = led_colors

    def build_state(self, serial: str, info: dict) -> controller_manager_pb2.ControllerState:
        """
        Build the controller's ControllerState from its live state, every call.

        There is no cache (#1675): a per-controller cache keyed on a snapshot of
        the fields cost about as much to key as the message costs to build, hit
        almost never with sensor noise, and served stale values whenever a field
        was missing from the key (#1635, #1638).

        Args:
            serial: Controller serial number
            info: Controller info dict

        Returns:
            ControllerState protobuf message
        """
        return self._build_controller_state_message(serial, info)

    def _build_controller_state_message(self, serial: str, info: dict) -> controller_manager_pb2.ControllerState:
        """
        Build a ControllerState protobuf message (Phase 18: Use pooled objects).

        Phase 57: Updated to use backend state dict instead of ControllerState object.

        Args:
            serial: Controller serial number
            info: Controller info dict

        Returns:
            ControllerState protobuf message
        """
        state_dict = self._controller_states.get(serial)

        if state_dict:
            trigger_pressed = state_dict.get(ButtonKey.TRIGGER, False)
            move_pressed = state_dict.get(ButtonKey.MOVE, False)
            cross_pressed = state_dict.get(ButtonKey.CROSS, False)
            circle_pressed = state_dict.get(ButtonKey.CIRCLE, False)
            square_pressed = state_dict.get(ButtonKey.SQUARE, False)
            triangle_pressed = state_dict.get(ButtonKey.TRIANGLE, False)
            ps_pressed = state_dict.get(ButtonKey.PS, False)
            select_pressed = state_dict.get(ButtonKey.SELECT, False)
            start_pressed = state_dict.get(ButtonKey.START, False)
            accel = state_dict.get(StateKey.ACCEL, {AxisKey.X: 0, AxisKey.Y: 0, AxisKey.Z: 0})
            gyro = state_dict.get(StateKey.GYRO, {AxisKey.X: 0, AxisKey.Y: 0, AxisKey.Z: 0})
        else:
            trigger_pressed = False
            move_pressed = False
            cross_pressed = False
            circle_pressed = False
            square_pressed = False
            triangle_pressed = False
            ps_pressed = False
            select_pressed = False
            start_pressed = False
            accel = {AxisKey.X: 0, AxisKey.Y: 0, AxisKey.Z: 0}
            gyro = {AxisKey.X: 0, AxisKey.Y: 0, AxisKey.Z: 0}

        # Use pooled Vector3 objects (Phase 18 - Task 3)
        accel_vec = self._vector3_pool.get()
        accel_vec.x = accel[AxisKey.X]
        accel_vec.y = accel[AxisKey.Y]
        accel_vec.z = accel[AxisKey.Z]

        gyro_vec = self._vector3_pool.get()
        gyro_vec.x = gyro[AxisKey.X]
        gyro_vec.y = gyro[AxisKey.Y]
        gyro_vec.z = gyro[AxisKey.Z]

        # Use pooled ControllerState (Phase 18 - Task 3)
        controller_state = self._controller_state_pool.get()
        controller_state.serial = serial
        controller_state.move_num = info.get(ControllerInfoKey.MOVE_NUM, 0)
        controller_state.battery = info.get(ControllerInfoKey.BATTERY, 0)
        controller_state.trigger_pressed = trigger_pressed
        controller_state.move_pressed = move_pressed
        controller_state.team = info.get(ControllerInfoKey.TEAM, 0)
        # The LED colour last written to the controller (#1301); off until the
        # first write.
        led_r, led_g, led_b = self._led_colors.get(serial, (0, 0, 0))
        controller_state.color.r = led_r
        controller_state.color.g = led_g
        controller_state.color.b = led_b
        controller_state.accel.CopyFrom(accel_vec)
        controller_state.gyro.CopyFrom(gyro_vec)
        controller_state.cross_pressed = cross_pressed
        controller_state.circle_pressed = circle_pressed
        controller_state.square_pressed = square_pressed
        controller_state.triangle_pressed = triangle_pressed
        controller_state.ps_pressed = ps_pressed
        controller_state.select_pressed = select_pressed
        controller_state.start_pressed = start_pressed
        controller_state.rssi = 0  # RSSI monitoring handled by host pairing-daemon
        controller_state.name = info.get(ControllerInfoKey.NAME, "")

        # Return pooled Vector3 objects (ControllerState made copies with CopyFrom)
        self._vector3_pool.return_msg(accel_vec)
        self._vector3_pool.return_msg(gyro_vec)

        return controller_state
