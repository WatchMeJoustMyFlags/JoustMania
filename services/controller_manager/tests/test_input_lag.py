"""
Tests for controller_input_lag_seconds (UC-035 step 6: input lag, queried in Grafana Explore).

What is measured: the age of the controller sample each gameplay frame carries,
i.e. the time from the controller-manager poll loop picking up a fresh sample
to that sample being written to StreamGameplayData. A stale replay (adapter
returns the same frame object again) keeps the old pick-up time, so a stalled
backend shows up as growing lag. Bluetooth/HID transport before the CM poll is
not included: SensorData carries no hardware timestamp. Only game streams
(which always send a controller filter) record it; the dashboard's unfiltered
stream does not.
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

from lib.controller_constants import ButtonKey
from proto import controller_manager_pb2
from services.controller_manager.discovery_loop import DiscoveryLoop
from services.controller_manager.multiplexer.press_latch import latch_presses, merge_latched_presses

LAG_METRIC = "services.controller_manager.metrics.controller_input_lag_seconds"
TIME_FN = "services.controller_manager.discovery_loop.time.time"
SERIAL = "AA:BB"


def _make_loop(backend) -> DiscoveryLoop:
    loop = DiscoveryLoop(
        backend=backend,
        tracked_controllers={SERIAL: {}},
        controller_states={},
        button_detector=MagicMock(),
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
    @pytest.mark.uc("UC-035", flow="main", steps=[6])
    def test_no_sample_yet_has_no_age(self):
        loop = _make_loop(MagicMock())
        assert loop.sample_age_seconds(SERIAL, 100.0) is None

    @pytest.mark.uc("UC-035", flow="main", steps=[6])
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

    @pytest.mark.uc("UC-035", flow="main", steps=[6])
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


class TestLatchedTapIsNotAFreshFrame:
    """#2352 B3: the tap-latch copy (UC-010 BR-012) must not reset the sample's pick-up time."""

    @pytest.mark.uc("UC-035", flow="main", steps=[6])
    @pytest.mark.asyncio
    async def test_latched_copy_keeps_aging_and_counts_no_fresh_frame(self):
        real = {"accel": {"x": 0, "y": 0, "z": 1}, ButtonKey.TRIGGER: False}
        latch: dict[str, set[str]] = {}
        backend = MagicMock()
        backend.get_controller_state = AsyncMock(side_effect=lambda _serial: merge_latched_presses(latch, SERIAL, real))
        loop = _make_loop(backend)

        await _poll_at(loop, 100.0)  # first sample
        fresh_before = loop._hw_fresh[SERIAL]

        # A tap landed between two polls, but the backend has no new sample: the poll returns a copy.
        latch_presses(latch, SERIAL, {ButtonKey.TRIGGER: True})
        await _poll_at(loop, 100.050)
        assert loop.controller_states[SERIAL][ButtonKey.TRIGGER] is True  # the tap reached the loop
        await _poll_at(loop, 100.060)  # the release poll: the real sample again

        assert loop._hw_fresh[SERIAL] == fresh_before
        assert loop.sample_age_seconds(SERIAL, 100.070) == pytest.approx(0.070)


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
            mock_loop.connection_quality = MagicMock(return_value=None)
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
            servicer.state_builder.build_state = MagicMock(return_value=state)
            servicer.tracked_controllers[SERIAL] = {}
            yield servicer, mock_loop

    @pytest.mark.uc("UC-035", flow="main", steps=[6])
    def test_frame_observes_sample_age_per_controller(self, servicer):
        servicer, mock_loop = servicer
        mock_loop.sample_age_seconds = MagicMock(return_value=0.012)

        with patch(LAG_METRIC) as lag:
            servicer._build_gameplay_update({SERIAL}, "game")
            servicer._build_gameplay_update({SERIAL}, "game")

        # Labelled child is looked up once per serial, then cached.
        lag.labels.assert_called_once_with(serial=SERIAL)
        assert lag.labels.return_value.observe.call_count == 2
        lag.labels.return_value.observe.assert_called_with(0.012)

    @pytest.mark.uc("UC-035", flow="main", steps=[6])
    def test_unfiltered_dashboard_stream_records_nothing(self, servicer):
        servicer, mock_loop = servicer
        mock_loop.sample_age_seconds = MagicMock(return_value=0.5)

        with patch(LAG_METRIC) as lag:
            update = servicer._build_gameplay_update(None, "dashboard")

        assert len(update.controllers) == 1
        lag.labels.return_value.observe.assert_not_called()

    @pytest.mark.uc("UC-035", flow="main", steps=[6])
    def test_pattern_controller_records_nothing(self, servicer):
        """Pattern controllers synthesise accel per delivered frame; the polled sample's age is meaningless."""
        servicer, mock_loop = servicer
        mock_loop.sample_age_seconds = MagicMock(return_value=0.012)

        class PatternBackend:
            def pattern_frame(self, _serial, _subscriber_id, _explicit):
                return {"x": 0.0, "y": 0.0, "z": 1.0}, False

            def pattern_serials(self):
                return {SERIAL}

        servicer.backend = PatternBackend()

        with patch(LAG_METRIC) as lag:
            servicer._build_gameplay_update({SERIAL}, "game")

        lag.labels.return_value.observe.assert_not_called()

    @pytest.mark.uc("UC-035", flow="main", steps=[6])
    def test_no_sample_yet_records_nothing(self, servicer):
        servicer, mock_loop = servicer
        mock_loop.sample_age_seconds = MagicMock(return_value=None)

        with patch(LAG_METRIC) as lag:
            servicer._build_gameplay_update({SERIAL}, "game")

        lag.labels.return_value.observe.assert_not_called()

    @pytest.mark.uc("UC-035", flow="main", steps=[6])
    def test_filtered_out_controller_records_nothing(self, servicer):
        servicer, mock_loop = servicer
        mock_loop.sample_age_seconds = MagicMock(return_value=0.012)

        with patch(LAG_METRIC) as lag:
            servicer._build_gameplay_update({"OTHER"})

        lag.labels.return_value.observe.assert_not_called()
