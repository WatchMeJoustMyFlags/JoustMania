"""
Tests for controller_input_lag_seconds (FR-083, UC-035 A2 HighInputLatency alert).

What is measured: the age of the controller sample each gameplay frame carries,
i.e. the time from the controller-manager poll loop picking up a fresh sample
to that sample being written to StreamGameplayData. A stale replay (adapter
returns the same frame object again) keeps the old pick-up time, so a stalled
backend shows up as growing lag. Bluetooth/HID transport before the CM poll is
not included: SensorData carries no hardware timestamp.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from proto import controller_manager_pb2
from services.controller_manager.discovery_loop import DiscoveryLoop

LAG_METRIC = "services.controller_manager.metrics.controller_input_lag_seconds"
TIME_FN = "services.controller_manager.discovery_loop.time.time"
SERIAL = "AA:BB"


def _make_loop(backend) -> DiscoveryLoop:
    loop = DiscoveryLoop(
        backend=backend,
        tracked_controllers={SERIAL: {}},
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
    loop.backend_initialized = True
    return loop


async def _poll_at(loop: DiscoveryLoop, now: float) -> None:
    with patch(TIME_FN, return_value=now):
        await loop._update_controller_states()


class TestSampleAge:
    @pytest.mark.uc("UC-035", flow="A2")
    def test_no_sample_yet_has_no_age(self):
        loop = _make_loop(MagicMock())
        assert loop.sample_age_seconds(SERIAL, 100.0) is None

    @pytest.mark.uc("UC-035", flow="A2")
    @pytest.mark.asyncio
    async def test_fresh_sample_age_counts_from_poll_pickup(self):
        backend = MagicMock()
        backend.get_controller_state = AsyncMock(side_effect=lambda _serial: {"accel": (0, 0, 1)})
        loop = _make_loop(backend)

        await _poll_at(loop, 100.0)
        assert loop.sample_age_seconds(SERIAL, 100.012) == pytest.approx(0.012)

        # A genuinely new frame resets the pick-up time.
        await _poll_at(loop, 100.010)
        assert loop.sample_age_seconds(SERIAL, 100.012) == pytest.approx(0.002)

    @pytest.mark.uc("UC-035", flow="A2")
    @pytest.mark.asyncio
    async def test_stale_replay_keeps_aging(self):
        """Same frame object returned again (stalled backend) is not a new sample."""
        frame = {"accel": (0, 0, 1)}
        backend = MagicMock()
        backend.get_controller_state = AsyncMock(return_value=frame)
        loop = _make_loop(backend)

        await _poll_at(loop, 100.0)
        await _poll_at(loop, 100.080)
        assert loop.sample_age_seconds(SERIAL, 100.090) == pytest.approx(0.090)


class TestGameplayFrameObservesLag:
    @pytest.fixture
    def servicer(self):
        with (
            patch("services.controller_manager.servicer.create_backend") as create_backend,
            patch("services.controller_manager.servicer.DiscoveryLoop") as discovery_cls,
        ):
            create_backend.return_value = MagicMock(spec=[])
            mock_loop = MagicMock()
            mock_loop.drain_health_counters = MagicMock(return_value=(0, 0, 0))
            mock_loop.drain_disconnects = MagicMock(return_value=[])
            discovery_cls.return_value = mock_loop

            from services.controller_manager.servicer import ControllerManagerServicer

            servicer = ControllerManagerServicer()
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
            servicer.state_cache_manager.build_or_get_cached_state = MagicMock(return_value=state)
            servicer.tracked_controllers[SERIAL] = {}
            yield servicer, mock_loop

    @pytest.mark.uc("UC-035", flow="A2")
    def test_frame_observes_sample_age_per_controller(self, servicer):
        servicer, mock_loop = servicer
        mock_loop.sample_age_seconds = MagicMock(return_value=0.012)

        with patch(LAG_METRIC) as lag:
            servicer._build_gameplay_update(None)

        lag.labels.assert_called_once_with(serial=SERIAL)
        lag.labels.return_value.observe.assert_called_once_with(0.012)

    @pytest.mark.uc("UC-035", flow="A2")
    def test_no_sample_yet_records_nothing(self, servicer):
        servicer, mock_loop = servicer
        mock_loop.sample_age_seconds = MagicMock(return_value=None)

        with patch(LAG_METRIC) as lag:
            servicer._build_gameplay_update(None)

        lag.labels.return_value.observe.assert_not_called()

    @pytest.mark.uc("UC-035", flow="A2")
    def test_filtered_out_controller_records_nothing(self, servicer):
        servicer, mock_loop = servicer
        mock_loop.sample_age_seconds = MagicMock(return_value=0.012)

        with patch(LAG_METRIC) as lag:
            servicer._build_gameplay_update({"OTHER"})

        lag.labels.return_value.observe.assert_not_called()
