"""Connection quality per controller (UC-035 BR-005, FR-122, #1431).

p95 gap <= 22 / <= 30 / > 30 ms and longest gap in a rolling 10 s window
<= 30 / <= 50 / > 50 ms rate good / fair / poor; the class is the worse of the two.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

test_dir = Path(__file__).parent
project_root = test_dir.parent.parent.parent
sys.path.insert(0, str(project_root))

from proto import controller_manager_pb2
from services.controller_manager.connection_quality import (
    FAIR,
    GOOD,
    POOR,
    QUALITY_LEVEL,
    GapStats,
    ReportGapWindow,
    classify,
    percentile_nearest_rank,
)
from services.controller_manager.discovery_loop import DiscoveryLoop
from services.controller_manager.tests.backend_doubles import autospec_backend, inert_hooks

SERIAL = "00:06:F7:AA:BB:CC"
TIME_FN = "services.controller_manager.discovery_loop.time.time"
MONO_FN = "services.controller_manager.discovery_loop.time.monotonic"
METRICS = "services.controller_manager.discovery_loop.metrics"


def _feed(window: ReportGapWindow, serial: str, start: float, gaps_ms: list[float]) -> float:
    """Record a fresh frame at ``start`` and one after each gap; returns the last timestamp."""
    now = start
    window.record(serial, now)
    for gap in gaps_ms:
        now += gap / 1000.0
        window.record(serial, now)
    return now


class TestClassificationBoundaries:
    @pytest.mark.uc("UC-035", br=["BR-005"])
    @pytest.mark.parametrize(
        ("p95_ms", "expected"),
        [(10.0, GOOD), (22.0, GOOD), (22.01, FAIR), (30.0, FAIR), (30.01, POOR), (120.0, POOR)],
    )
    def test_p95_gap_thresholds(self, p95_ms, expected):
        # Longest gap kept good so only the p95 rating decides.
        assert classify(p95_ms, min(p95_ms, 30.0)) == expected

    @pytest.mark.uc("UC-035", br=["BR-005"])
    @pytest.mark.parametrize(
        ("max_ms", "expected"),
        [(10.0, GOOD), (30.0, GOOD), (30.01, FAIR), (50.0, FAIR), (50.01, POOR), (500.0, POOR)],
    )
    def test_longest_gap_thresholds(self, max_ms, expected):
        assert classify(10.0, max_ms) == expected

    @pytest.mark.uc("UC-035", br=["BR-005"])
    @pytest.mark.parametrize(
        ("p95_ms", "max_ms", "expected"),
        [(25.0, 20.0, FAIR), (20.0, 45.0, FAIR), (25.0, 60.0, POOR), (35.0, 40.0, POOR)],
    )
    def test_class_is_the_worse_of_the_two(self, p95_ms, max_ms, expected):
        assert classify(p95_ms, max_ms) == expected

    @pytest.mark.uc("UC-035", br=["BR-005"])
    def test_levels_grow_with_badness(self):
        assert QUALITY_LEVEL[GOOD] < QUALITY_LEVEL[FAIR] < QUALITY_LEVEL[POOR]

    @pytest.mark.uc("UC-035", br=["BR-005"])
    def test_p95_is_nearest_rank(self):
        values = [float(v) for v in range(1, 101)]
        assert percentile_nearest_rank(values, 0.95) == 95.0
        assert percentile_nearest_rank([7.0], 0.95) == 7.0
        # 20 samples: rank ceil(19) = 19th value.
        assert percentile_nearest_rank([float(v) for v in range(1, 21)], 0.95) == 19.0


class TestRollingWindow:
    @pytest.mark.uc("UC-035", br=["BR-005"])
    def test_fewer_than_two_fresh_frames_is_unknown(self):
        window = ReportGapWindow()
        assert window.snapshot(SERIAL, 100.0) is None
        window.record(SERIAL, 100.0)
        assert window.snapshot(SERIAL, 100.0) is None

    @pytest.mark.uc("UC-035", br=["BR-005"])
    def test_steady_ten_ms_frames_are_good(self):
        window = ReportGapWindow()
        last = _feed(window, SERIAL, 100.0, [10.0] * 200)
        stats = window.snapshot(SERIAL, last)
        assert stats == GapStats(p95_ms=pytest.approx(10.0), max_ms=pytest.approx(10.0), quality=GOOD, samples=200)

    @pytest.mark.uc("UC-035", br=["BR-005"])
    def test_one_long_gap_makes_the_controller_poor_until_it_leaves_the_window(self):
        window = ReportGapWindow()
        last = _feed(window, SERIAL, 100.0, [10.0] * 50 + [80.0] + [10.0] * 50)
        stats = window.snapshot(SERIAL, last)
        assert stats.max_ms == pytest.approx(80.0)
        assert stats.p95_ms == pytest.approx(10.0)
        assert stats.quality == POOR

        # 10 s of steady frames later the 80 ms gap has left the window.
        last = _feed(window, SERIAL, last, [10.0] * 1001)
        stats = window.snapshot(SERIAL, last)
        assert stats.max_ms == pytest.approx(10.0)
        assert stats.quality == GOOD

    @pytest.mark.uc("UC-035", br=["BR-005"])
    def test_p95_of_alternating_gaps(self):
        # 10 / 20 ms alternation (an 80 Hz controller seen through a 100 Hz poll) is good;
        # one 25 ms gap in every 10 lifts p95 to fair.
        window = ReportGapWindow()
        last = _feed(window, SERIAL, 100.0, [10.0, 20.0] * 100)
        assert window.snapshot(SERIAL, last).quality == GOOD

        window = ReportGapWindow()
        last = _feed(window, SERIAL, 100.0, ([10.0] * 8 + [25.0, 25.0]) * 20)
        stats = window.snapshot(SERIAL, last)
        assert stats.p95_ms == pytest.approx(25.0)
        assert stats.quality == FAIR

    @pytest.mark.uc("UC-035", br=["BR-005"])
    def test_open_gap_counts_as_longest_gap(self):
        window = ReportGapWindow()
        last = _feed(window, SERIAL, 100.0, [10.0] * 100)
        stats = window.snapshot(SERIAL, last + 0.2)
        assert stats.max_ms == pytest.approx(200.0)
        assert stats.quality == POOR

    @pytest.mark.uc("UC-035", br=["BR-005"])
    def test_silent_for_the_whole_window_stays_poor(self):
        window = ReportGapWindow()
        last = _feed(window, SERIAL, 100.0, [10.0] * 10)
        stats = window.snapshot(SERIAL, last + 11.0)
        assert stats.quality == POOR
        assert stats.max_ms == pytest.approx(11000.0)
        assert stats.samples == 0

    @pytest.mark.uc("UC-035", br=["BR-005"])
    def test_one_frame_long_ago_is_poor_one_frame_recently_unknown(self):
        window = ReportGapWindow()
        window.record(SERIAL, 100.0)
        assert window.snapshot(SERIAL, 105.0) is None
        assert window.snapshot(SERIAL, 110.0).quality == POOR

    @pytest.mark.uc("UC-035", br=["BR-005"])
    def test_loop_stall_is_not_charged_to_the_link(self):
        # Poll loop stalls 45 ms between two pick-ups: the 55 ms elapsed is 10 ms of link gap.
        window = ReportGapWindow()
        last = _feed(window, SERIAL, 100.0, [10.0] * 50)
        window.note_loop_stall(0.045)
        window.record(SERIAL, last + 0.055)
        stats = window.snapshot(SERIAL, last + 0.055)
        assert stats.max_ms == pytest.approx(10.0)
        assert stats.quality == GOOD

    @pytest.mark.uc("UC-035", br=["BR-005"])
    def test_stalls_are_subtracted_from_every_gap_they_span(self):
        window = ReportGapWindow()
        window.record(SERIAL, 100.0)
        window.record("OTHER", 100.0)
        window.note_loop_stall(0.020)
        window.record(SERIAL, 100.040)  # 40 ms elapsed, 20 ms of it the loop's
        window.note_loop_stall(0.030)
        window.record("OTHER", 100.090)  # 90 ms elapsed spanning both stalls
        assert window.snapshot(SERIAL, 100.040).max_ms == pytest.approx(20.0)
        assert window.snapshot("OTHER", 100.090).max_ms == pytest.approx(40.0)
        # Negative lateness (loop ahead of schedule) is ignored.
        window.note_loop_stall(-0.005)
        window.record(SERIAL, 100.050)
        assert window.snapshot(SERIAL, 100.050).p95_ms == pytest.approx(20.0)

    @pytest.mark.uc("UC-035", br=["BR-005"])
    def test_forget_drops_the_controller(self):
        window = ReportGapWindow()
        last = _feed(window, SERIAL, 100.0, [10.0] * 10)
        window.forget(SERIAL)
        assert window.snapshot(SERIAL, last) is None
        assert window.serials() == []


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
    with patch(TIME_FN, return_value=now), patch(MONO_FN, return_value=now):
        await loop._update_controller_states()


class TestDiscoveryLoopExport:
    @pytest.fixture
    def loop(self):
        backend = inert_hooks(MagicMock(spec=["get_controller_state", "get_connected_controllers"]))
        # A new dict per poll: every poll is a fresh frame.
        backend.get_controller_state = AsyncMock(side_effect=lambda _serial: {"accel": (0, 0, 1)})
        return _make_loop(backend)

    @pytest.mark.uc("UC-035", br=["BR-005"])
    @pytest.mark.asyncio
    async def test_health_emission_exports_gap_stats_and_class(self, loop):
        # Polls every 10 ms; the link delivers nothing new for 60 ms once (stale replays).
        stale = {"accel": (0, 0, 1)}
        frames = [{"accel": (0, 0, 1)} for _ in range(20)] + [stale] * 6 + [{"accel": (0, 0, 1)}]
        loop.backend.get_controller_state = AsyncMock(side_effect=frames)
        for i in range(len(frames)):
            await _poll_at(loop, 100.0 + i * 0.010)

        with patch(METRICS) as metrics:
            loop._emit_bluetooth_health(100.26, 100.26)

        stats = loop.connection_quality(SERIAL)
        assert stats.quality == POOR
        assert stats.max_ms == pytest.approx(60.0)
        metrics.controller_bluetooth_report_gap_p95_seconds.labels.assert_called_with(serial=SERIAL)
        metrics.controller_bluetooth_report_gap_p95_seconds.labels.return_value.set.assert_called_with(
            pytest.approx(0.010)
        )
        metrics.controller_bluetooth_report_gap_max_seconds.labels.return_value.set.assert_called_with(
            pytest.approx(0.060)
        )
        metrics.controller_connection_quality.labels.return_value.set.assert_called_with(QUALITY_LEVEL[POOR])

    @pytest.mark.uc("UC-035", br=["BR-005"])
    @pytest.mark.asyncio
    async def test_poll_loop_stall_does_not_rate_the_link_worse(self, loop):
        # Fresh frame on every poll, but the loop itself stalls 80 ms once.
        times = [100.0 + i * 0.010 for i in range(20)]
        times += [times[-1] + 0.090 + i * 0.010 for i in range(20)]
        for t in times:
            await _poll_at(loop, t)
        with patch(METRICS):
            loop._emit_bluetooth_health(times[-1], times[-1])

        stats = loop.connection_quality(SERIAL)
        assert stats.max_ms == pytest.approx(10.0)
        assert stats.quality == GOOD

    @pytest.mark.uc("UC-035", br=["BR-005"])
    @pytest.mark.asyncio
    async def test_poll_loop_stall_is_subtracted_from_the_health_event_gap(self, loop):
        # Same 80 ms loop stall: the health span's bluetooth.event_gap_ms stays at the 10 ms link gap.
        times = [100.0 + i * 0.010 for i in range(20)]
        times += [times[-1] + 0.090 + i * 0.010 for i in range(20)]
        for t in times:
            await _poll_at(loop, t)

        signals = loop._compute_health_window(window_seconds=times[-1] - times[0])

        assert signals["event_gap_ms"] == pytest.approx(10.0)

    @pytest.mark.uc("UC-035", br=["BR-005"])
    @pytest.mark.asyncio
    async def test_span_event_carries_the_quality(self, loop):
        for i in range(5):
            await _poll_at(loop, 100.0 + i * 0.010)

        span = MagicMock()
        tracer = MagicMock()
        tracer.start_as_current_span.return_value.__enter__.return_value = span
        with patch(METRICS), patch("services.controller_manager.discovery_loop.tracer", tracer):
            loop._emit_bluetooth_health(100.04, 100.04)

        name, attrs = span.add_event.call_args.args
        assert name == "bluetooth_controller_sample"
        assert attrs["bluetooth.connection_quality"] == GOOD
        assert attrs["bluetooth.report_gap_p95_ms"] == pytest.approx(10.0)
        assert attrs["bluetooth.report_gap_max_ms"] == pytest.approx(10.0)

    @pytest.mark.uc("UC-035", br=["BR-005"])
    @pytest.mark.asyncio
    async def test_single_frame_exports_no_quality(self, loop):
        await _poll_at(loop, 100.0)
        with patch(METRICS) as metrics:
            loop._emit_bluetooth_health(100.0, 100.0)

        assert loop.connection_quality(SERIAL) is None
        metrics.controller_connection_quality.labels.assert_not_called()
        metrics.controller_connection_quality.remove.assert_called_with(SERIAL)

    @pytest.mark.uc("UC-035", br=["BR-005"])
    @pytest.mark.asyncio
    async def test_untracked_controller_drops_its_quality(self, loop):
        for i in range(5):
            await _poll_at(loop, 100.0 + i * 0.010)
        with patch(METRICS):
            loop._emit_bluetooth_health(100.04, 100.04)
        assert loop.connection_quality(SERIAL) is not None

        # Discovery no longer sees the controller: the disconnect path untracks it.
        loop.backend.get_connected_controllers = MagicMock(return_value=[])
        loop.rescan_timer.should_force_rescan = MagicMock(return_value=False)
        with patch(METRICS) as metrics:
            await loop._check_for_new_controllers()

        assert SERIAL not in loop.tracked_controllers
        assert loop.connection_quality(SERIAL) is None
        metrics.controller_connection_quality.remove.assert_called_with(SERIAL)
        metrics.controller_bluetooth_report_gap_p95_seconds.remove.assert_called_with(SERIAL)
        metrics.controller_bluetooth_report_gap_max_seconds.remove.assert_called_with(SERIAL)
        assert loop._gap_window.snapshot(SERIAL, 100.05) is None


class TestGameplayFrameCarriesQuality:
    @pytest.fixture
    def servicer(self):
        with (
            patch("services.controller_manager.servicer.create_backend") as create_backend,
            patch("services.controller_manager.servicer.DiscoveryLoop") as discovery_cls,
        ):
            create_backend.return_value = autospec_backend()
            mock_loop = MagicMock()
            mock_loop.drain_health_counters = MagicMock(return_value=(0, 0, 0))
            mock_loop.drain_disconnects = MagicMock(return_value=[])
            mock_loop.sample_age_seconds = MagicMock(return_value=None)
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

    @pytest.mark.uc("UC-035", flow="main", steps=[2], br=["BR-005"])
    def test_dashboard_stream_carries_class_and_gaps(self, servicer):
        servicer, mock_loop = servicer
        mock_loop.connection_quality = MagicMock(
            return_value=GapStats(p95_ms=24.0, max_ms=41.0, quality=FAIR, samples=900)
        )

        update = servicer._build_gameplay_update(None, "dashboard")

        quality = update.controllers[0].connection_quality
        assert update.controllers[0].HasField("connection_quality")
        assert quality.level == "fair"
        assert quality.report_gap_p95_ms == pytest.approx(24.0)
        assert quality.report_gap_max_ms == pytest.approx(41.0)

    @pytest.mark.uc("UC-035", flow="main", steps=[2], br=["BR-005"])
    def test_message_is_built_once_per_stats_update(self, servicer):
        servicer, mock_loop = servicer
        first = GapStats(p95_ms=10.0, max_ms=12.0, quality=GOOD, samples=900)
        mock_loop.connection_quality = MagicMock(return_value=first)
        msg_a = servicer._connection_quality_msg(SERIAL)
        assert servicer._connection_quality_msg(SERIAL) is msg_a

        mock_loop.connection_quality.return_value = GapStats(p95_ms=31.0, max_ms=60.0, quality=POOR, samples=900)
        msg_b = servicer._connection_quality_msg(SERIAL)
        assert msg_b is not msg_a
        assert msg_b.level == "poor"

    @pytest.mark.uc("UC-035", flow="main", steps=[2], br=["BR-005"])
    def test_unknown_quality_leaves_the_field_unset(self, servicer):
        servicer, mock_loop = servicer
        mock_loop.connection_quality = MagicMock(return_value=None)

        update = servicer._build_gameplay_update(None, "dashboard")

        assert not update.controllers[0].HasField("connection_quality")

    @pytest.mark.uc("UC-035", br=["BR-005"])
    def test_game_stream_does_not_carry_it(self, servicer):
        servicer, mock_loop = servicer
        mock_loop.connection_quality = MagicMock(
            return_value=GapStats(p95_ms=10.0, max_ms=10.0, quality=GOOD, samples=900)
        )

        update = servicer._build_gameplay_update({SERIAL}, "game")

        assert not update.controllers[0].HasField("connection_quality")
        mock_loop.connection_quality.assert_not_called()
