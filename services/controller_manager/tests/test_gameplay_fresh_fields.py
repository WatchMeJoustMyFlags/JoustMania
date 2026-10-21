"""UC-030 step 2, BR-002: every gameplay frame carries the latest value of every field (#1675).

A per-controller state cache keyed on a hand-written snapshot of the fields served stale values
twice when a field was missing from the key (#1635, #1638), and served accel/gyro changes below
0.005 from the cache. Each frame is now built from the live state. These tests drive the real
servicer frame builder: change one source value between two frames and expect the second frame
to carry it. ``test_every_stateful_field_is_covered`` fails when a new ``GameplayData`` field is
added without a case here.
"""

from collections.abc import Callable
from unittest.mock import create_autospec, patch

import pytest

from lib.controller_constants import AxisKey, ButtonKey, ControllerInfoKey, StateKey
from proto import controller_manager_pb2
from services.controller_manager.backend import ControllerBackend

SERIAL = "AA:BB:CC:DD:EE:01"

# GameplayData fields that do not come from the controller's state or info: the serial is the
# key, rssi is fixed at 0 (D-C5), health and connection_quality are drained per frame elsewhere.
NOT_FROM_STATE = {"serial", "rssi", "health", "connection_quality"}


@pytest.fixture
def servicer():
    backend = create_autospec(ControllerBackend, instance=True)
    with patch("services.controller_manager.servicer.create_backend", return_value=backend):
        from services.controller_manager.servicer import ControllerManagerServicer

        svc = ControllerManagerServicer()
    svc.tracked_controllers[SERIAL] = {
        ControllerInfoKey.SERIAL: SERIAL,
        ControllerInfoKey.BATTERY: 4,
        ControllerInfoKey.TEAM: 1,
        ControllerInfoKey.MOVE_NUM: 1,
        ControllerInfoKey.NAME: "Old Name",
    }
    svc.controller_states[SERIAL] = {
        StateKey.ACCEL: {AxisKey.X: 0.001, AxisKey.Y: 0.0, AxisKey.Z: 1.0},
        StateKey.GYRO: {AxisKey.X: 0.001, AxisKey.Y: 0.0, AxisKey.Z: 0.0},
    }
    svc.feedback_manager.current_colors[SERIAL] = (10, 20, 30)
    return svc


def _info(svc) -> dict:
    return svc.tracked_controllers[SERIAL]


def _state(svc) -> dict:
    return svc.controller_states[SERIAL]


def _set_axis(key: StateKey, axis: AxisKey, value: float) -> Callable:
    def change(svc) -> None:
        # A fresh dict per poll, as the adapters return.
        _state(svc)[key] = {**_state(svc)[key], axis: value}

    return change


def _set_trigger(svc) -> None:
    _state(svc)[ButtonKey.TRIGGER] = True


def _set_info(key: ControllerInfoKey, value) -> Callable:
    def change(svc) -> None:
        _info(svc)[key] = value

    return change


def _set_colour(svc) -> None:
    svc.feedback_manager.current_colors[SERIAL] = (200, 100, 50)


def _rgb(msg) -> tuple[int, int, int]:
    return (msg.r, msg.g, msg.b)


def _xyz(msg) -> tuple[float, float, float]:
    return (msg.x, msg.y, msg.z)


# (GameplayData field, change to the source, read the field, expected value after the change)
CASES = [
    ("move_num", _set_info(ControllerInfoKey.MOVE_NUM, 2), lambda gd: gd.move_num, 2),
    ("battery", _set_info(ControllerInfoKey.BATTERY, 2), lambda gd: gd.battery, 2),
    ("team", _set_info(ControllerInfoKey.TEAM, 0), lambda gd: gd.team, 0),
    ("name", _set_info(ControllerInfoKey.NAME, "New Name"), lambda gd: gd.name, "New Name"),
    ("color", _set_colour, lambda gd: _rgb(gd.color), (200, 100, 50)),
    # Changes below 0.005 used to be served from the cache (2-decimal key).
    ("accel", _set_axis(StateKey.ACCEL, AxisKey.X, 0.004), lambda gd: _xyz(gd.accel), (0.004, 0.0, 1.0)),
    ("trigger_pressed", _set_trigger, lambda gd: gd.trigger_pressed, True),  # #1837
    ("gyro", _set_axis(StateKey.GYRO, AxisKey.X, 0.004), lambda gd: _xyz(gd.gyro), (0.004, 0.0, 0.0)),
]


def _frame(svc):
    (data,) = svc._build_gameplay_update(None).controllers
    return data


@pytest.mark.uc("UC-030", flow="main", steps=[2], br=["BR-002"])
@pytest.mark.parametrize(("field", "change", "read", "expected"), CASES, ids=[c[0] for c in CASES])
def test_next_frame_carries_the_changed_field(servicer, field, change, read, expected):
    """UC-030 step 2: a value changed between two frames is in the second frame."""
    before = read(_frame(servicer))
    assert before != pytest.approx(expected), f"{field}: the case must change the value"

    change(servicer)

    assert read(_frame(servicer)) == pytest.approx(expected)


@pytest.mark.uc("UC-030", flow="main", steps=[2], br=["BR-002"])
def test_every_stateful_field_is_covered():
    """A new GameplayData field needs a case above (or an entry in NOT_FROM_STATE)."""
    fields = {f.name for f in controller_manager_pb2.GameplayData.DESCRIPTOR.fields}

    assert fields - NOT_FROM_STATE == {c[0] for c in CASES}
