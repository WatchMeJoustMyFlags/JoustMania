"""
Per-controller gameplay frame inputs for ControllerManager.

Reads the live controller state into the plain values one GameplayData needs
(#2353). The gameplay frame builds its GameplayData straight from them: no
ControllerState message, no pooled Vector3 copies (the per-frame state cache was
removed in #1675, the ControllerState pool in #1708, the Vector3 pool in #2353).
"""

import logging
from typing import TYPE_CHECKING, NamedTuple

from lib.controller_constants import (
    AxisKey,
    ButtonKey,
    ControllerInfoKey,
    StateKey,
)

if TYPE_CHECKING:
    from services.controller_manager.monitoring import ControllerMonitoring

logger = logging.getLogger(__name__)


class FrameInputs(NamedTuple):
    """The values one controller contributes to a GameplayData (#2353).

    ``color``, ``accel`` and ``gyro`` are fresh dicts keyed by the proto field names, so they
    go straight into ``GameplayData(...)``: a dict for a message field always sets it, so an
    all-zero vector or an unlit LED is still present on the wire, as with the old CopyFrom path.
    """

    serial: str
    move_num: int
    battery: int
    team: int
    color: dict
    accel: dict
    gyro: dict
    rssi: int
    name: str
    trigger_pressed: bool


class StateBuilder:
    """Reads the live controller state for the gameplay frame (no cache, #1675)."""

    def __init__(self, monitoring: "ControllerMonitoring"):
        """
        Initialize the state builder.

        Args:
            monitoring: ControllerMonitoring instance for RSSI data
        """
        self.monitoring = monitoring

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

    def frame_inputs(self, serial: str, info: dict) -> FrameInputs:
        """
        Read the controller's frame values from its live state, every call.

        There is no cache (#1675): a per-controller cache keyed on a snapshot of
        the fields cost about as much to key as the message costs to build, hit
        almost never with sensor noise, and served stale values whenever a field
        was missing from the key (#1635, #1638).

        The state dict is the poll's sample (also referenced by the adapter and
        compared by identity for freshness): it is only read here, never mutated
        or kept; every value is copied out before this returns.

        Args:
            serial: Controller serial number
            info: Controller info dict

        Returns:
            FrameInputs with fresh dicts for color, accel and gyro
        """
        state_dict = self._controller_states.get(serial)
        if state_dict:
            trigger_pressed = bool(state_dict.get(ButtonKey.TRIGGER, False))
            accel = state_dict.get(StateKey.ACCEL, _ZERO_AXES)
            gyro = state_dict.get(StateKey.GYRO, _ZERO_AXES)
        else:
            trigger_pressed = False
            accel = gyro = _ZERO_AXES

        # The LED colour last written to the controller (#1301); off until the first write.
        led_r, led_g, led_b = self._led_colors.get(serial, (0, 0, 0))
        info_get = info.get
        return FrameInputs(
            serial=serial,
            move_num=info_get(ControllerInfoKey.MOVE_NUM, 0),
            battery=info_get(ControllerInfoKey.BATTERY, 0),
            team=info_get(ControllerInfoKey.TEAM, 0),
            color={"r": led_r, "g": led_g, "b": led_b},
            accel={"x": accel[AxisKey.X], "y": accel[AxisKey.Y], "z": accel[AxisKey.Z]},
            gyro={"x": gyro[AxisKey.X], "y": gyro[AxisKey.Y], "z": gyro[AxisKey.Z]},
            rssi=0,  # RSSI monitoring handled by host pairing-daemon (D-C5)
            name=info_get(ControllerInfoKey.NAME, ""),
            trigger_pressed=trigger_pressed,
        )


# Read-only default for a missing sample or motion key; frame_inputs copies its values out.
_ZERO_AXES = {AxisKey.X: 0, AxisKey.Y: 0, AxisKey.Z: 0}
