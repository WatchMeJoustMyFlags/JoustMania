"""UC-030 step 2, BR-002: the 60 Hz gameplay frame is byte-identical after the #2353 refactor.

#2353 builds each GameplayData straight from the state dict instead of building a ControllerState
(with pooled Vector3 copies) and copying it into a GameplayData. These tests pin the wire bytes:

- ``GOLDEN_*`` are the serialized frames the pre-#2353 path produced for the scenario below
  (captured on origin/main 1fc68f83, timestamp zeroed);
- ``_legacy_gameplay_data`` is a frozen copy of that pre-#2353 per-controller path, compared
  controller by controller, so a later scenario change still checks against the old builder.

The scenario covers buttons, motion (values not exact in float32), battery, team, move number,
name, LED colour, a controller with no hardware sample yet (all-zero vectors must still be
present on the wire), a sample without motion keys, a motion pattern override, the trigger
latch, health counters, connection quality and a disconnect.
"""

from unittest.mock import create_autospec, patch

import pytest

from lib.controller_constants import AxisKey, ButtonKey, ControllerInfoKey, StateKey
from proto import controller_manager_pb2
from services.controller_manager.backend import ControllerBackend
from services.controller_manager.tests.backend_doubles import stub_method

FULL = "AA:BB:CC:DD:EE:01"  # every field set, health counters pending
BARE = "AA:BB:CC:DD:EE:02"  # no hardware sample, empty info, no LED write; trigger latched
NO_MOTION = "AA:BB:CC:DD:EE:03"  # a sample without accel/gyro keys
PATTERN = "mock_controller_7"  # motion pattern override with a policy press
SERIALS = (FULL, BARE, NO_MOTION, PATTERN)

# Serialized GameplayDataUpdate (timestamp = 0) from the pre-#2353 builder for _scenario().
GOLDEN_GAME_FRAME = (
    "0a560a1141413a42423a43433a44443a45453a3031100218042801320708c801102d18033a0f0dcd"
    "cccc3d15cdcc4cbe1d48e17a3f420f0d0ad7233c15cdcc4c3d1d45d8703d520a506c61796572204f"
    "6e655a04080218010a1b0a1141413a42423a43433a44443a45453a303232003a00420068010a2d0a"
    "1141413a42423a43433a44443a45453a303310011805320308ff013a00420052094e6f204d6f7469"
    "6f6e68010a200a116d6f636b5f636f6e74726f6c6c65725f3732003a051d00002040420068011a04"
    "0a024744"
)
GOLDEN_OBSERVER_FRAME = (
    "0a5d0a1141413a42423a43433a44443a45453a3031100218042801320708c801102d18033a0f0dcd"
    "cccc3d15cdcc4cbe1d48e17a3f420f0d0ad7233c15cdcc4c3d1d45d8703d520a506c61796572204f"
    "6e65620b0a046661697215000048410a190a1141413a42423a43433a44443a45453a303232003a00"
    "42000a2d0a1141413a42423a43433a44443a45453a303310011805320308ff013a00420052094e6f"
    "204d6f74696f6e68010a200a116d6f636b5f636f6e74726f6c6c65725f3732003a051d0000204042"
    "006801"
)


def _scenario():
    """Servicer with real StateBuilder and frame loop; only collaborators outside the frame are stubbed."""
    backend = create_autospec(ControllerBackend, instance=True)
    backend.pattern_serials.return_value = {PATTERN}
    backend.pattern_frame.side_effect = lambda serial, _sid, _explicit: (
        ({"x": 0.0, "y": 0.0, "z": 2.5}, True) if serial == PATTERN else None
    )
    backend.consume_trigger_press.side_effect = lambda serial: serial == BARE
    backend.consume_death_frame.return_value = None
    with patch("services.controller_manager.servicer.create_backend", return_value=backend):
        from services.controller_manager.servicer import ControllerManagerServicer

        svc = ControllerManagerServicer()

    svc.tracked_controllers[FULL] = {
        ControllerInfoKey.SERIAL: FULL,
        ControllerInfoKey.BATTERY: 4,
        ControllerInfoKey.TEAM: 1,
        ControllerInfoKey.MOVE_NUM: 2,
        ControllerInfoKey.NAME: "Player One",
    }
    svc.tracked_controllers[BARE] = {}
    svc.tracked_controllers[NO_MOTION] = {
        ControllerInfoKey.BATTERY: 5,
        ControllerInfoKey.TEAM: 0,
        ControllerInfoKey.MOVE_NUM: 1,
        ControllerInfoKey.NAME: "No Motion",
    }
    svc.tracked_controllers[PATTERN] = {ControllerInfoKey.NAME: ""}

    svc.controller_states[FULL] = {
        ButtonKey.TRIGGER: False,
        ButtonKey.MOVE: True,
        ButtonKey.CROSS: True,
        ButtonKey.PS: False,
        ButtonKey.SELECT: True,
        StateKey.ACCEL: {AxisKey.X: 0.1, AxisKey.Y: -0.2, AxisKey.Z: 0.98},
        StateKey.GYRO: {AxisKey.X: 0.01, AxisKey.Y: 0.05, AxisKey.Z: 0.0588},
        StateKey.BATTERY: 4,
    }
    svc.controller_states[NO_MOTION] = {ButtonKey.TRIGGER: True}
    svc.controller_states[PATTERN] = {
        StateKey.ACCEL: {AxisKey.X: 0.1, AxisKey.Y: 0.1, AxisKey.Z: 1.0},
        StateKey.GYRO: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 0.0},
    }
    svc.feedback_manager.current_colors[FULL] = (200, 45, 3)
    svc.feedback_manager.current_colors[NO_MOTION] = (255, 0, 0)

    stub_method(
        svc.discovery_loop,
        "drain_health_counters",
        side_effect=lambda serial: (2, 0, 1) if serial == FULL else (0, 0, 0),
    )
    stub_method(svc, "_take_disconnects", return_value=["GD"])
    quality = controller_manager_pb2.ConnectionQuality(level="fair", report_gap_p95_ms=12.5)
    stub_method(svc, "_connection_quality_msg", side_effect=lambda serial: quality if serial == FULL else None)
    svc._open_game_stream("game", set(SERIALS))
    return svc


def _wire(update: controller_manager_pb2.GameplayDataUpdate) -> str:
    update.timestamp = 0
    return update.SerializeToString().hex()


def _legacy_gameplay_data(svc, serial, *, accel_override, trigger, health, connection_quality):
    """Frozen copy of the pre-#2353 per-controller build: ControllerState + Vector3, then GameplayData."""
    state_dict = svc.controller_states.get(serial)
    info = svc.tracked_controllers[serial]
    zero = {AxisKey.X: 0, AxisKey.Y: 0, AxisKey.Z: 0}
    if state_dict:
        trigger_pressed = state_dict.get(ButtonKey.TRIGGER, False)
        accel = state_dict.get(StateKey.ACCEL, zero)
        gyro = state_dict.get(StateKey.GYRO, zero)
    else:
        trigger_pressed, accel, gyro = False, zero, zero
    accel_vec = controller_manager_pb2.Vector3(x=accel[AxisKey.X], y=accel[AxisKey.Y], z=accel[AxisKey.Z])
    gyro_vec = controller_manager_pb2.Vector3(x=gyro[AxisKey.X], y=gyro[AxisKey.Y], z=gyro[AxisKey.Z])
    cs = controller_manager_pb2.ControllerState()
    cs.serial = serial
    cs.move_num = info.get(ControllerInfoKey.MOVE_NUM, 0)
    cs.battery = info.get(ControllerInfoKey.BATTERY, 0)
    cs.trigger_pressed = trigger_pressed
    cs.team = info.get(ControllerInfoKey.TEAM, 0)
    led_r, led_g, led_b = svc.feedback_manager.current_colors.get(serial, (0, 0, 0))
    cs.color.r, cs.color.g, cs.color.b = led_r, led_g, led_b
    cs.accel.CopyFrom(accel_vec)
    cs.gyro.CopyFrom(gyro_vec)
    cs.rssi = 0
    cs.name = info.get(ControllerInfoKey.NAME, "")
    return controller_manager_pb2.GameplayData(
        serial=cs.serial,
        move_num=cs.move_num,
        battery=cs.battery,
        team=cs.team,
        color=cs.color,
        accel=accel_override if accel_override is not None else cs.accel,
        gyro=cs.gyro,
        rssi=cs.rssi,
        name=cs.name,
        health=health,
        connection_quality=connection_quality,
        trigger_pressed=cs.trigger_pressed or trigger,
    )


class TestUC030FrameBytesUnchangedBy2353:
    @pytest.mark.uc("UC-030", flow="main", steps=[2], br=["BR-002"])
    def test_game_stream_frame_matches_the_golden_bytes(self):
        """A game stream frame (health, trigger latch, pattern, disconnect) serializes as before #2353."""
        svc = _scenario()
        assert _wire(svc._build_gameplay_update(set(SERIALS), "game")) == GOLDEN_GAME_FRAME

    @pytest.mark.uc("UC-030", flow="main", steps=[2], br=["BR-002"])
    def test_observer_frame_matches_the_golden_bytes(self):
        """The dashboard's unfiltered frame (connection quality, no drains) serializes as before #2353."""
        svc = _scenario()
        assert _wire(svc._build_gameplay_update(None, "dashboard")) == GOLDEN_OBSERVER_FRAME

    @pytest.mark.uc("UC-030", flow="main", steps=[2], br=["BR-002"])
    def test_each_controller_matches_the_pre_2353_builder(self):
        """Controller by controller, the new build equals the frozen pre-#2353 build, byte for byte."""
        svc = _scenario()
        game = {gd.serial: gd for gd in svc._build_gameplay_update(set(SERIALS), "game").controllers}
        expected = {
            FULL: {"health": controller_manager_pb2.ControllerHealth(poll_drops=2, led_failures=1)},
            BARE: {"trigger": True},
            NO_MOTION: {},
            PATTERN: {"accel_override": controller_manager_pb2.Vector3(x=0.0, y=0.0, z=2.5), "trigger": True},
        }
        assert set(game) == set(SERIALS)
        for serial, overrides in expected.items():
            legacy = _legacy_gameplay_data(
                svc,
                serial,
                accel_override=overrides.get("accel_override"),
                trigger=overrides.get("trigger", False),
                health=overrides.get("health"),
                connection_quality=None,
            )
            assert game[serial].SerializeToString() == legacy.SerializeToString(), serial
