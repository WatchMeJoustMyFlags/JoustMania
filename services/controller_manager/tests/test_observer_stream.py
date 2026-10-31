"""
An observer gameplay stream never consumes game signals (UC-035 BR-006, #1595).

The dashboard's unfiltered stream (connect-proxy WatchGameplayData) shares the
controller manager with the game's filtered stream. Disconnects (#580), health
counters and the mock death latch (#926) are read once, so only the game
stream may drain them; an observer frame in between must leave them for it.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, create_autospec, patch

import pytest

test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from lib.controller_constants import AxisKey
from proto import controller_manager_pb2
from services.controller_manager.button_detector import ButtonDetector
from services.controller_manager.discovery import PeriodicRescanTimer
from services.controller_manager.discovery_loop import DiscoveryLoop
from services.controller_manager.event_publisher import EventPublisher
from services.controller_manager.feedback_manager import FeedbackManager
from services.controller_manager.monitoring import ControllerMonitoring
from services.controller_manager.multiplexer.mock_adapter import MockAdapter
from services.controller_manager.servicer import _stream_rate_hz
from services.controller_manager.tests.backend_doubles import autospec_backend

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
    state.trigger_pressed = False
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

    backend = autospec_backend()
    backend.consume_trigger_press.return_value = False
    backend.pattern_serials.return_value = set()
    backend.pattern_frame.return_value = None
    backend.pattern_stream_rate.return_value = None
    backend.consume_death_frame.side_effect = adapter.consume_death_frame

    with (
        patch("services.controller_manager.servicer.create_backend", return_value=backend),
        patch("services.controller_manager.servicer.DiscoveryLoop"),
    ):
        from services.controller_manager.servicer import ControllerManagerServicer

        servicer = ControllerManagerServicer()

    monitoring = create_autospec(ControllerMonitoring, instance=True)
    monitoring.last_battery_check = 0
    loop = DiscoveryLoop(
        backend=autospec_backend(),
        tracked_controllers=servicer.tracked_controllers,
        controller_states={},
        button_detector=create_autospec(ButtonDetector, instance=True),
        feedback_manager=create_autospec(FeedbackManager, instance=True),
        monitoring=monitoring,
        rescan_timer=create_autospec(PeriodicRescanTimer, instance=True),
        paired_serials=[],
        base_colors={},
        event_publisher=create_autospec(EventPublisher, instance=True),
    )
    servicer.discovery_loop = loop
    servicer.state_builder.build_state = MagicMock(return_value=_state())
    servicer.tracked_controllers[SERIAL] = {}

    loop._record(SERIAL).poll_drops = 2
    loop._record(SERIAL).poll_errors = 1
    return servicer, ctrl


@pytest.mark.uc("UC-035", br=["BR-006"])
def test_observer_frame_leaves_game_signals_for_the_game_stream(rig):
    servicer, ctrl = rig

    servicer._open_game_stream("game", {SERIAL})  # the game stream sent colours
    servicer.discovery_loop._gameplay_disconnects.append("GONE")

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
def test_game_stream_with_no_player_alive_still_drains(rig):
    """An empty alive filter (NonStop respawn window, reconnect with 0 alive) is still the game."""
    servicer, ctrl = rig
    servicer._open_game_stream("game", {SERIAL})
    servicer.discovery_loop._gameplay_disconnects.append("GONE")

    # _process_filter_update maps an empty serial list to None.
    game = servicer._build_gameplay_update(None, "game")

    assert [d.serial for d in game.disconnects] == ["GONE"]
    health = game.controllers[0].health
    assert (health.poll_drops, health.poll_errors) == (2, 1)
    assert ctrl["death_frames_remaining"] == 2


@pytest.mark.uc("UC-035", br=["BR-006"])
@pytest.mark.parametrize(
    ("requested", "game_stream", "override", "expected"),
    [
        (30, False, 60, 30),  # dashboard asks 30, flag 60: observer stays at 30
        (60, False, 30, 30),  # observer capped at the flag rate
        (0, False, 60, 1),  # never a zero or negative interval
        (30, True, 60, 60),  # a game stream runs at the flag rate
        (30, False, None, 30),  # no flag: the requested rate
        (45, True, None, 45),
    ],
)
def test_stream_rate(requested, game_stream, override, expected):
    assert _stream_rate_hz(requested, game_stream, override) == expected


async def _frame_intervals(servicer, config, frames: int = 6) -> list[float]:
    """Run StreamGameplayData for a few frames and return the gaps between its sleeps."""
    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        await real_sleep(0)  # let the stream's config reader run

    async def requests():
        yield controller_manager_pb2.GameplayStreamControl(config=config)
        await real_sleep(3600)

    context = MagicMock()
    context.cancelled.return_value = False
    with patch("services.controller_manager.servicer.asyncio.sleep", side_effect=fake_sleep):
        stream = servicer.StreamGameplayData(requests(), context)
        for _ in range(frames):
            await stream.__anext__()
        await stream.aclose()
    # Sleeps follow a fixed schedule, so successive sleep times differ by one interval.
    return [b - a for a, b in zip(sleeps[-3:], sleeps[-2:], strict=False)]


@pytest.mark.asyncio
@pytest.mark.uc("UC-035", br=["BR-006"])
async def test_stream_loop_applies_the_per_stream_rate(rig):
    """The send loop uses _stream_rate_hz: observer at its 30 Hz request, game at the 60 Hz flag."""
    servicer, _ = rig
    servicer._ensure_discovery_started = MagicMock()
    feedback = create_autospec(FeedbackManager, instance=True)
    feedback.base_colors = {}
    servicer.feedback_manager = feedback

    observer_config = controller_manager_pb2.GameplayStreamConfig(update_frequency_hz=30)
    game_config = controller_manager_pb2.GameplayStreamConfig(
        update_frequency_hz=30,
        colors=[controller_manager_pb2.ControllerColorConfig(serial=SERIAL)],
    )
    with patch("services.controller_manager.servicer._frequency_override", 60):
        observer = await _frame_intervals(servicer, observer_config)
        game = await _frame_intervals(servicer, game_config)

    assert observer == pytest.approx([1 / 30, 1 / 30], abs=0.003)
    assert game == pytest.approx([1 / 60, 1 / 60], abs=0.003)
