"""
Unit tests for controller health span tracking on player_lifecycle (#571).

Tests:
- _process_health() opens/closes child spans on degradation transitions
- Rolling window rate detection: spans open when drop rate exceeds threshold
- Events are added to child spans per frame with drop_rate attribute
- Summary attributes are set on player_lifecycle span on close
- Healthy game produces no health child spans
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import MockControllerManagerService, async_noop

from proto import controller_manager_pb2
from services.game_coordinator.games.base import Player
from services.game_coordinator.games.ffa import FFAGame


class MockGameplayStream:
    """Mock bidirectional stream for testing."""

    def __init__(self):
        self.messages = []

    async def write(self, message):
        self.messages.append(message)


def _make_game():
    """Create a minimal FFA game for testing _process_health."""
    mock_cm = MockControllerManagerService(num_controllers=2)
    game = FFAGame(
        controller_manager_client=mock_cm,
        event_publisher=async_noop,
        audio_client=None,
        game_id="test_health",
    )
    game.gameplay_stream = MockGameplayStream()
    return game


def _make_player(serial="AA:BB:CC", with_span=True):
    """Create a Player with optional mock span."""
    player = Player(serial=serial)
    if with_span:
        player.span = MagicMock()
    return player


def _make_gameplay_data(serial="AA:BB:CC", poll_drops=0, poll_errors=0, led_failures=0):
    """Create a GameplayData proto with health counters."""
    health = None
    if poll_drops or poll_errors or led_failures:
        health = controller_manager_pb2.ControllerHealth(
            poll_drops=poll_drops,
            poll_errors=poll_errors,
            led_failures=led_failures,
        )
    return controller_manager_pb2.GameplayData(
        serial=serial,
        accel=controller_manager_pb2.Vector3(x=0, y=0, z=1.0),
        gyro=controller_manager_pb2.Vector3(x=0, y=0, z=0),
        health=health,
    )


class TestProcessHealthPollDegradation:
    """Tests for poll degradation child span lifecycle using rolling window."""

    @patch("services.game_coordinator.games.base.time")
    @patch("services.game_coordinator.games.base.get_config_manager")
    @patch("services.game_coordinator.games.base.tracer")
    def test_opens_span_after_window_with_high_rate(self, mock_tracer, mock_get_config, mock_time):
        """Span should open when rolling drop rate exceeds threshold after a full window."""
        mock_get_config.return_value.get_config.return_value.poll_drop_threshold = 10
        game = _make_game()
        player = _make_player()
        mock_child_span = MagicMock()
        mock_tracer.start_span.return_value = mock_child_span

        # Frame 1 at t=0: initialize window, drops accumulate but no rate yet
        # Window starts with _health_window_drops = total_poll_drops after accumulation
        mock_time.monotonic.return_value = 0.0
        gd1 = _make_gameplay_data(poll_drops=2)
        game._process_health(player, "AA:BB:CC", gd1)
        # Rate is still 0.0 (window not elapsed), no span
        mock_tracer.start_span.assert_not_called()

        # Intermediate frames adding drops within the window
        mock_time.monotonic.return_value = 1.0
        gd2 = _make_gameplay_data(poll_drops=12)
        game._process_health(player, "AA:BB:CC", gd2)
        mock_tracer.start_span.assert_not_called()

        mock_time.monotonic.return_value = 1.5
        gd3 = _make_gameplay_data(poll_drops=12)
        game._process_health(player, "AA:BB:CC", gd3)
        mock_tracer.start_span.assert_not_called()

        # Frame at t=2.0: window elapses
        # total_poll_drops = 2 + 12 + 12 = 26
        # _health_window_drops = 2 (set at init after first frame's accumulation)
        # drops_in_window = 26 - 2 = 24, rate = 24 / 2.0 = 12.0 drops/sec > 10
        mock_time.monotonic.return_value = 2.0
        gd4 = _make_gameplay_data(poll_drops=0)
        game._process_health(player, "AA:BB:CC", gd4)

        mock_tracer.start_span.assert_called_once_with(
            "controller_poll_degraded",
            context=mock_tracer.start_span.call_args.kwargs["context"],
            attributes={"player.serial": "AA:BB:CC"},
        )
        assert player._poll_degraded_span is mock_child_span
        assert player.total_poll_drops == 26
        assert player._health_drop_rate == 12.0

    @patch("services.game_coordinator.games.base.time")
    @patch("services.game_coordinator.games.base.get_config_manager")
    @patch("services.game_coordinator.games.base.tracer")
    def test_no_span_on_first_frame_with_accumulated_drops(self, mock_tracer, mock_get_config, mock_time):
        """First frame should NOT open span even with high drops (pre-game accumulation)."""
        mock_get_config.return_value.get_config.return_value.poll_drop_threshold = 10
        game = _make_game()
        player = _make_player()

        # Single frame at t=0 with many drops (simulates pre-game dump)
        mock_time.monotonic.return_value = 0.0
        gd = _make_gameplay_data(poll_drops=100)
        game._process_health(player, "AA:BB:CC", gd)

        # Rate is 0.0 on first frame (window just initialized), no span
        mock_tracer.start_span.assert_not_called()
        assert player._poll_degraded_span is None
        assert player.total_poll_drops == 100

    @patch("services.game_coordinator.games.base.time")
    @patch("services.game_coordinator.games.base.get_config_manager")
    @patch("services.game_coordinator.games.base.tracer")
    def test_no_span_when_rate_below_threshold(self, mock_tracer, mock_get_config, mock_time):
        """Low drop rate over window should NOT open a degraded span."""
        mock_get_config.return_value.get_config.return_value.poll_drop_threshold = 10
        game = _make_game()
        player = _make_player()

        # Frame 1 at t=0: init window (baseline = 0 since no drops yet)
        mock_time.monotonic.return_value = 0.0
        gd1 = _make_gameplay_data(poll_drops=0)
        game._process_health(player, "AA:BB:CC", gd1)

        # Frame 2 at t=1.0: a few drops (below threshold rate)
        mock_time.monotonic.return_value = 1.0
        gd2 = _make_gameplay_data(poll_drops=3)
        game._process_health(player, "AA:BB:CC", gd2)

        # Frame 3 at t=2.0: window elapses
        # drops_in_window = 3 - 0 = 3, rate = 3/2.0 = 1.5 drops/sec < 10
        mock_time.monotonic.return_value = 2.0
        gd3 = _make_gameplay_data(poll_drops=2)
        game._process_health(player, "AA:BB:CC", gd3)

        mock_tracer.start_span.assert_not_called()
        assert player._poll_degraded_span is None
        assert player._health_drop_rate == 2.5

    @patch("services.game_coordinator.games.base.time")
    @patch("services.game_coordinator.games.base.get_config_manager")
    @patch("services.game_coordinator.games.base.tracer")
    def test_adds_event_per_frame_with_drop_rate(self, mock_tracer, mock_get_config, mock_time):
        """Each frame with issues should add an event with drop_rate to the child span."""
        mock_get_config.return_value.get_config.return_value.poll_drop_threshold = 10
        game = _make_game()
        player = _make_player()
        mock_child_span = MagicMock()
        mock_tracer.start_span.return_value = mock_child_span

        # Frame at t=0: init window, baseline drops absorbed
        mock_time.monotonic.return_value = 0.0
        gd1 = _make_gameplay_data(poll_drops=0)
        game._process_health(player, "AA:BB:CC", gd1)

        # Add drops during the window (these will count)
        mock_time.monotonic.return_value = 0.5
        gd_mid1 = _make_gameplay_data(poll_drops=12)
        game._process_health(player, "AA:BB:CC", gd_mid1)

        mock_time.monotonic.return_value = 1.0
        gd_mid2 = _make_gameplay_data(poll_drops=15)
        game._process_health(player, "AA:BB:CC", gd_mid2)

        # t=2.0: window elapses
        # total_poll_drops = 0 + 12 + 15 + 5 = 32, window_drops = 0
        # drops_in_window = 32 - 0 = 32, rate = 32/2.0 = 16.0 > 10 -> opens span
        mock_time.monotonic.return_value = 2.0
        gd2 = _make_gameplay_data(poll_drops=5)
        game._process_health(player, "AA:BB:CC", gd2)
        assert mock_tracer.start_span.call_count == 1

        # Next frame at t=2.5: span already open, add another event
        mock_time.monotonic.return_value = 2.5
        gd3 = _make_gameplay_data(poll_drops=6, poll_errors=1)
        game._process_health(player, "AA:BB:CC", gd3)

        # Should only open span once
        assert mock_tracer.start_span.call_count == 1
        # Should have two events (one from window evaluation, one from next frame)
        assert mock_child_span.add_event.call_count == 2

        # First event should include drop_rate and poll_drops
        first_call_attrs = mock_child_span.add_event.call_args_list[0]
        assert first_call_attrs[0][0] == "poll_issues"
        assert first_call_attrs[0][1]["drop_rate"] == 16.0
        assert first_call_attrs[0][1]["poll_drops"] == 5

        # Second event should include drop_rate, poll_drops, and poll_errors
        second_call_attrs = mock_child_span.add_event.call_args_list[1]
        assert second_call_attrs[0][0] == "poll_issues"
        assert second_call_attrs[0][1]["drop_rate"] == 16.0
        assert second_call_attrs[0][1]["poll_drops"] == 6
        assert second_call_attrs[0][1]["poll_errors"] == 1

    @patch("services.game_coordinator.games.base.time")
    @patch("services.game_coordinator.games.base.get_config_manager")
    @patch("services.game_coordinator.games.base.tracer")
    def test_closes_span_on_recovery(self, mock_tracer, mock_get_config, mock_time):
        """Healthy rate after degradation should close the child span."""
        mock_get_config.return_value.get_config.return_value.poll_drop_threshold = 10
        game = _make_game()
        player = _make_player()
        mock_child_span = MagicMock()
        mock_tracer.start_span.return_value = mock_child_span

        # t=0: init window with no drops (baseline = 0)
        mock_time.monotonic.return_value = 0.0
        gd0 = _make_gameplay_data(poll_drops=0)
        game._process_health(player, "AA:BB:CC", gd0)

        # Add drops during window
        mock_time.monotonic.return_value = 1.0
        gd1 = _make_gameplay_data(poll_drops=15)
        game._process_health(player, "AA:BB:CC", gd1)

        mock_time.monotonic.return_value = 1.5
        gd1b = _make_gameplay_data(poll_drops=10)
        game._process_health(player, "AA:BB:CC", gd1b)

        # t=2.0: window elapses, drops_in_window = 25 - 0 = 25, rate = 12.5 > 10
        mock_time.monotonic.return_value = 2.0
        gd2 = _make_gameplay_data(poll_drops=0)
        game._process_health(player, "AA:BB:CC", gd2)
        assert player._poll_degraded_span is not None

        # t=4.0: next window, no new drops -> rate = 0/2.0 = 0.0 < 10 -> recovery
        mock_time.monotonic.return_value = 4.0
        gd3 = _make_gameplay_data()
        game._process_health(player, "AA:BB:CC", gd3)

        assert player._poll_degraded_span is None
        mock_child_span.set_attribute.assert_any_call("health.total_poll_drops", 25)
        mock_child_span.set_attribute.assert_any_call("health.total_poll_errors", 0)
        mock_child_span.set_status.assert_called_once()
        mock_child_span.end.assert_called_once()

    @patch("services.game_coordinator.games.base.time")
    @patch("services.game_coordinator.games.base.get_config_manager")
    @patch("services.game_coordinator.games.base.tracer")
    def test_no_span_without_player_span(self, mock_tracer, mock_get_config, mock_time):
        """No child span should be created if player has no parent span."""
        mock_get_config.return_value.get_config.return_value.poll_drop_threshold = 10
        game = _make_game()
        player = _make_player(with_span=False)

        # t=0: init with no drops, then add drops during window
        mock_time.monotonic.return_value = 0.0
        gd0 = _make_gameplay_data(poll_drops=0)
        game._process_health(player, "AA:BB:CC", gd0)

        mock_time.monotonic.return_value = 1.0
        gd1 = _make_gameplay_data(poll_drops=30)
        game._process_health(player, "AA:BB:CC", gd1)

        # t=2.0: rate = 30/2.0 = 15.0 > 10 -> would open span, but no parent span
        mock_time.monotonic.return_value = 2.0
        gd2 = _make_gameplay_data(poll_drops=0)
        game._process_health(player, "AA:BB:CC", gd2)

        mock_tracer.start_span.assert_not_called()
        assert player._poll_degraded_span is None
        # But counters should still accumulate
        assert player.total_poll_drops == 30

    @patch("services.game_coordinator.games.base.time")
    @patch("services.game_coordinator.games.base.get_config_manager")
    @patch("services.game_coordinator.games.base.tracer")
    def test_poll_errors_open_span_immediately(self, mock_tracer, mock_get_config, mock_time):
        """Poll errors should open span regardless of drop rate."""
        mock_get_config.return_value.get_config.return_value.poll_drop_threshold = 10
        game = _make_game()
        player = _make_player()
        mock_child_span = MagicMock()
        mock_tracer.start_span.return_value = mock_child_span

        # First frame with poll_errors but no high drop rate
        mock_time.monotonic.return_value = 0.0
        gd = _make_gameplay_data(poll_errors=1)
        game._process_health(player, "AA:BB:CC", gd)

        mock_tracer.start_span.assert_called_once()
        assert player._poll_degraded_span is mock_child_span


class TestProcessHealthLedDegradation:
    """Tests for LED degradation child span lifecycle."""

    @patch("services.game_coordinator.games.base.time")
    @patch("services.game_coordinator.games.base.get_config_manager")
    @patch("services.game_coordinator.games.base.tracer")
    def test_opens_led_span_on_failures(self, mock_tracer, mock_get_config, mock_time):
        """LED failures should open a controller_led_degraded span."""
        mock_get_config.return_value.get_config.return_value.poll_drop_threshold = 10
        mock_time.monotonic.return_value = 0.0
        game = _make_game()
        player = _make_player()
        mock_child_span = MagicMock()
        mock_tracer.start_span.return_value = mock_child_span

        gd = _make_gameplay_data(led_failures=2)
        game._process_health(player, "AA:BB:CC", gd)

        mock_tracer.start_span.assert_called_once_with(
            "controller_led_degraded",
            context=mock_tracer.start_span.call_args.kwargs["context"],
            attributes={"player.serial": "AA:BB:CC"},
        )
        assert player._led_degraded_span is mock_child_span
        assert player.total_led_failures == 2

    @patch("services.game_coordinator.games.base.time")
    @patch("services.game_coordinator.games.base.get_config_manager")
    @patch("services.game_coordinator.games.base.tracer")
    def test_closes_led_span_on_recovery(self, mock_tracer, mock_get_config, mock_time):
        """Healthy frame should close LED degradation span."""
        mock_get_config.return_value.get_config.return_value.poll_drop_threshold = 10
        mock_time.monotonic.return_value = 0.0
        game = _make_game()
        player = _make_player()
        mock_child_span = MagicMock()
        mock_tracer.start_span.return_value = mock_child_span

        # Degraded
        gd1 = _make_gameplay_data(led_failures=2)
        game._process_health(player, "AA:BB:CC", gd1)

        # Healthy
        mock_time.monotonic.return_value = 0.5
        gd2 = _make_gameplay_data()
        game._process_health(player, "AA:BB:CC", gd2)

        assert player._led_degraded_span is None
        mock_child_span.set_attribute.assert_called_with("health.total_led_failures", 2)
        mock_child_span.end.assert_called_once()


class TestProcessHealthCombined:
    """Tests for combined poll + LED degradation."""

    @patch("services.game_coordinator.games.base.time")
    @patch("services.game_coordinator.games.base.get_config_manager")
    @patch("services.game_coordinator.games.base.tracer")
    def test_both_poll_and_led_spans(self, mock_tracer, mock_get_config, mock_time):
        """Both poll rate degradation and LED issues should open separate child spans."""
        mock_get_config.return_value.get_config.return_value.poll_drop_threshold = 10
        game = _make_game()
        player = _make_player()
        mock_poll_span = MagicMock(name="poll_span")
        mock_led_span = MagicMock(name="led_span")
        mock_tracer.start_span.side_effect = [mock_poll_span, mock_led_span]

        # t=0: init window with no drops (baseline = 0)
        mock_time.monotonic.return_value = 0.0
        gd0 = _make_gameplay_data(poll_drops=0)
        game._process_health(player, "AA:BB:CC", gd0)

        # Add drops during window
        mock_time.monotonic.return_value = 1.0
        gd1 = _make_gameplay_data(poll_drops=30)
        game._process_health(player, "AA:BB:CC", gd1)

        # t=2.0: window elapses, drops_in_window = 30 - 0 = 30, rate = 15 > 10
        # Also LED failure on this frame
        mock_time.monotonic.return_value = 2.0
        gd2 = _make_gameplay_data(poll_drops=0, led_failures=2)
        game._process_health(player, "AA:BB:CC", gd2)

        assert mock_tracer.start_span.call_count == 2
        assert player._poll_degraded_span is mock_poll_span
        assert player._led_degraded_span is mock_led_span

    @patch("services.game_coordinator.games.base.time")
    @patch("services.game_coordinator.games.base.get_config_manager")
    @patch("services.game_coordinator.games.base.tracer")
    def test_healthy_game_no_spans(self, mock_tracer, mock_get_config, mock_time):
        """Healthy data should create no child spans."""
        mock_get_config.return_value.get_config.return_value.poll_drop_threshold = 10
        game = _make_game()
        player = _make_player()

        for i in range(10):
            mock_time.monotonic.return_value = i * 0.5
            gd = _make_gameplay_data()
            game._process_health(player, "AA:BB:CC", gd)

        mock_tracer.start_span.assert_not_called()
        assert player.total_poll_drops == 0
        assert player.total_poll_errors == 0
        assert player.total_led_failures == 0


class TestFinalizePlayerHealth:
    """Tests for _finalize_player_health called during span close."""

    def test_closes_open_health_spans_on_game_end(self):
        """Open health spans should be closed by _finalize_player_health."""
        game = _make_game()

        mock_span = MagicMock()
        player = Player(serial="S1")
        player.span = mock_span
        mock_poll_span = MagicMock()
        mock_led_span = MagicMock()
        player._poll_degraded_span = mock_poll_span
        player._led_degraded_span = mock_led_span
        player.total_poll_drops = 42
        player.total_poll_errors = 5
        player.total_led_failures = 3

        game._finalize_player_health(player)

        # Health spans should be closed with error status
        mock_poll_span.set_attribute.assert_any_call("health.total_poll_drops", 42)
        mock_poll_span.set_attribute.assert_any_call("health.total_poll_errors", 5)
        mock_poll_span.set_status.assert_called_once()
        mock_poll_span.end.assert_called_once()

        mock_led_span.set_attribute.assert_called_with("health.total_led_failures", 3)
        mock_led_span.end.assert_called_once()

        # Span references should be cleared
        assert player._poll_degraded_span is None
        assert player._led_degraded_span is None

    def test_summary_attributes_with_issues(self):
        """health.had_issues should be True when there were issues."""
        game = _make_game()

        mock_span = MagicMock()
        player = Player(serial="S1")
        player.span = mock_span
        player.total_poll_drops = 10
        player._health_drop_rate = 5.0

        game._finalize_player_health(player)

        mock_span.set_attribute.assert_any_call("health.total_poll_drops", 10)
        mock_span.set_attribute.assert_any_call("health.total_poll_errors", 0)
        mock_span.set_attribute.assert_any_call("health.total_led_failures", 0)
        mock_span.set_attribute.assert_any_call("health.final_drop_rate", 5.0)
        mock_span.set_attribute.assert_any_call("health.had_issues", True)

    def test_summary_attributes_healthy(self):
        """health.had_issues should be False when no issues."""
        game = _make_game()

        mock_span = MagicMock()
        player = Player(serial="S1")
        player.span = mock_span

        game._finalize_player_health(player)

        mock_span.set_attribute.assert_any_call("health.had_issues", False)
        mock_span.set_attribute.assert_any_call("health.final_drop_rate", 0.0)

    def test_no_health_spans_nothing_to_close(self):
        """When no health spans are open, finalize should still work."""
        game = _make_game()

        mock_span = MagicMock()
        player = Player(serial="S1")
        player.span = mock_span

        # Should not raise
        game._finalize_player_health(player)

        # Summary attributes should still be set
        mock_span.set_attribute.assert_any_call("health.total_poll_drops", 0)

    def test_no_op_without_player_span(self):
        """Finalize should be a no-op if player has no span."""
        game = _make_game()
        player = Player(serial="S1")
        player.total_poll_drops = 10

        # Should not raise
        game._finalize_player_health(player)


class TestPlayerDataclassHealth:
    """Tests that Player dataclass has correct health defaults."""

    def test_default_health_counters_zero(self):
        player = Player(serial="S1")
        assert player.total_poll_drops == 0
        assert player.total_poll_errors == 0
        assert player.total_led_failures == 0

    def test_default_health_spans_none(self):
        player = Player(serial="S1")
        assert player._poll_degraded_span is None
        assert player._led_degraded_span is None

    def test_default_rolling_window_fields(self):
        player = Player(serial="S1")
        assert player._health_window_start is None
        assert player._health_window_drops == 0
        assert player._health_drop_rate == 0.0
