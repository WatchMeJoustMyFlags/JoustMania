"""
An observer gameplay stream never consumes game signals (UC-035 BR-006, #1595).

The dashboard's unfiltered stream (connect-proxy WatchGameplayData) shares the
controller manager with the game's filtered stream. Disconnects (#580), health
counters and the mock death latch (#926) are read once, so only the game
stream may drain them; an observer frame in between must leave them for it.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from lib.controller_constants import AxisKey
from proto import controller_manager_pb2
from services.controller_manager.discovery_loop import DiscoveryLoop
from services.controller_manager.multiplexer.mock_adapter import MockAdapter
from services.controller_manager.servicer import _stream_rate_hz

SERIAL = "mock_controller_0"


def _state() -> MagicMock:
    state = MagicMock()
    state.serial = SERIAL
    state.move_num = 0
    state.battery = 80
    state.team = 0
    state.color = controller_manager_pb2.RGB(r=255, g=0, b=0)
    state.accel = controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0)
    state.gyro = controller_manager_pb2.Vector3(x=0.0, y=0.0, z=0.0)
    state.rssi = 0
    state.name = "P1"
    return state


@pytest.fixture
def rig():
    """Servicer with a real DiscoveryLoop (drain queues) and a real mock death latch."""
    adapter = MockAdapter(num_controllers=1)
    adapter.discover()
    ctrl = adapter.controllers[SERIAL]
    ctrl["death_accel"] = {AxisKey.X: 5.0, AxisKey.Y: 3.0, AxisKey.Z: 4.0}
    ctrl["death_frames_remaining"] = 3
    ctrl["death_hold_until"] = 0.0

    backend = MagicMock(spec=["consume_death_frame"])
    backend.consume_death_frame.side_effect = adapter.consume_death_frame

    with (
        patch("services.controller_manager.servicer.create_backend", return_value=backend),
        patch("services.controller_manager.servicer.DiscoveryLoop"),
    ):
        from services.controller_manager.servicer import ControllerManagerServicer

        servicer = ControllerManagerServicer()

    loop = DiscoveryLoop(
        backend=MagicMock(spec=[]),
        tracked_controllers=servicer.tracked_controllers,
        controller_states={},
        button_detector=MagicMock(),
        state_cache_manager=MagicMock(),
        feedback_manager=MagicMock(),
        monitoring=MagicMock(last_battery_check=0),
        rescan_timer=MagicMock(),
        paired_serials=[],
        base_colors={},
        event_publisher=MagicMock(),
    )
    servicer.discovery_loop = loop
    servicer.state_cache_manager.build_or_get_cached_state = MagicMock(return_value=_state())
    servicer.tracked_controllers[SERIAL] = {}

    loop._gameplay_disconnects.append("GONE")
    loop._poll_drops[SERIAL] = 2
    loop._poll_errors[SERIAL] = 1
    return servicer, ctrl


@pytest.mark.uc("UC-035", br=["BR-006"])
def test_observer_frame_leaves_game_signals_for_the_game_stream(rig):
    servicer, ctrl = rig

    observer = servicer._build_gameplay_update(None, "dashboard")
    game = servicer._build_gameplay_update({SERIAL}, "game")

    # The observer frame carries none of the read-once signals ...
    assert list(observer.disconnects) == []
    assert not observer.controllers[0].HasField("health")
    # ... and the game frame still gets all of them.
    assert [d.serial for d in game.disconnects] == ["GONE"]
    health = game.controllers[0].health
    assert (health.poll_drops, health.poll_errors) == (2, 1)
    # Only the game frame counted against the death latch: 3 -> 2.
    assert ctrl["death_frames_remaining"] == 2


@pytest.mark.uc("UC-035", br=["BR-006"])
@pytest.mark.parametrize(
    ("requested", "current_filter", "override", "expected"),
    [
        (30, None, 60, 30),  # dashboard asks 30, flag 60: observer stays at 30
        (60, None, 30, 30),  # observer capped at the flag rate
        (0, None, 60, 1),  # never a zero or negative interval
        (30, {SERIAL}, 60, 60),  # a game stream runs at the flag rate
        (30, None, None, 30),  # no flag: the requested rate
        (45, {SERIAL}, None, 45),
    ],
)
def test_stream_rate(requested, current_filter, override, expected):
    assert _stream_rate_hz(requested, current_filter, override) == expected
