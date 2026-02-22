"""
Unit tests for controller health span tracking on player_lifecycle (#571).

Tests:
- _process_health() opens/closes child spans on degradation transitions
- Events are added to child spans per frame
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
    """Tests for poll degradation child span lifecycle."""

    @patch("services.game_coordinator.games.base.tracer")
    def test_opens_span_on_first_poll_drops(self, mock_tracer):
        """First frame with poll_drops should open a controller_poll_degraded span."""
        game = _make_game()
        player = _make_player()
        mock_child_span = MagicMock()
        mock_tracer.start_span.return_value = mock_child_span

        gd = _make_gameplay_data(poll_drops=3)
        game._process_health(player, "AA:BB:CC", gd)

        mock_tracer.start_span.assert_called_once_with(
            "controller_poll_degraded",
            context=mock_tracer.start_span.call_args.kwargs["context"],
            attributes={"player.serial": "AA:BB:CC"},
        )
        assert player._poll_degraded_span is mock_child_span
        assert player.total_poll_drops == 3

    @patch("services.game_coordinator.games.base.tracer")
    def test_adds_event_per_frame(self, mock_tracer):
        """Each frame with issues should add an event to the child span."""
        game = _make_game()
        player = _make_player()
        mock_child_span = MagicMock()
        mock_tracer.start_span.return_value = mock_child_span

        # Frame 1: opens span + adds event
        gd1 = _make_gameplay_data(poll_drops=3)
        game._process_health(player, "AA:BB:CC", gd1)

        # Frame 2: should add another event (span already open)
        gd2 = _make_gameplay_data(poll_drops=5, poll_errors=1)
        game._process_health(player, "AA:BB:CC", gd2)

        # Should only open span once
        assert mock_tracer.start_span.call_count == 1
        # Should have two events
        assert mock_child_span.add_event.call_count == 2
        mock_child_span.add_event.assert_any_call("poll_issues", {"poll_drops": 3})
        mock_child_span.add_event.assert_any_call("poll_issues", {"poll_drops": 5, "poll_errors": 1})

        assert player.total_poll_drops == 8
        assert player.total_poll_errors == 1

    @patch("services.game_coordinator.games.base.tracer")
    def test_closes_span_on_recovery(self, mock_tracer):
        """Healthy frame after degradation should close the child span."""
        game = _make_game()
        player = _make_player()
        mock_child_span = MagicMock()
        mock_tracer.start_span.return_value = mock_child_span

        # Frame 1: degraded
        gd1 = _make_gameplay_data(poll_drops=3)
        game._process_health(player, "AA:BB:CC", gd1)
        assert player._poll_degraded_span is not None

        # Frame 2: healthy (no health field)
        gd2 = _make_gameplay_data()
        game._process_health(player, "AA:BB:CC", gd2)

        assert player._poll_degraded_span is None
        mock_child_span.set_attribute.assert_any_call("health.total_poll_drops", 3)
        mock_child_span.set_attribute.assert_any_call("health.total_poll_errors", 0)
        mock_child_span.end.assert_called_once()

    @patch("services.game_coordinator.games.base.tracer")
    def test_no_span_without_player_span(self, mock_tracer):
        """No child span should be created if player has no parent span."""
        game = _make_game()
        player = _make_player(with_span=False)

        gd = _make_gameplay_data(poll_drops=3)
        game._process_health(player, "AA:BB:CC", gd)

        mock_tracer.start_span.assert_not_called()
        assert player._poll_degraded_span is None
        # But counters should still accumulate
        assert player.total_poll_drops == 3


class TestProcessHealthLedDegradation:
    """Tests for LED degradation child span lifecycle."""

    @patch("services.game_coordinator.games.base.tracer")
    def test_opens_led_span_on_failures(self, mock_tracer):
        """LED failures should open a controller_led_degraded span."""
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

    @patch("services.game_coordinator.games.base.tracer")
    def test_closes_led_span_on_recovery(self, mock_tracer):
        """Healthy frame should close LED degradation span."""
        game = _make_game()
        player = _make_player()
        mock_child_span = MagicMock()
        mock_tracer.start_span.return_value = mock_child_span

        # Degraded
        gd1 = _make_gameplay_data(led_failures=2)
        game._process_health(player, "AA:BB:CC", gd1)

        # Healthy
        gd2 = _make_gameplay_data()
        game._process_health(player, "AA:BB:CC", gd2)

        assert player._led_degraded_span is None
        mock_child_span.set_attribute.assert_called_with("health.total_led_failures", 2)
        mock_child_span.end.assert_called_once()


class TestProcessHealthCombined:
    """Tests for combined poll + LED degradation."""

    @patch("services.game_coordinator.games.base.tracer")
    def test_both_poll_and_led_spans(self, mock_tracer):
        """Both poll and LED issues should open separate child spans."""
        game = _make_game()
        player = _make_player()
        mock_poll_span = MagicMock(name="poll_span")
        mock_led_span = MagicMock(name="led_span")
        mock_tracer.start_span.side_effect = [mock_poll_span, mock_led_span]

        gd = _make_gameplay_data(poll_drops=3, led_failures=2)
        game._process_health(player, "AA:BB:CC", gd)

        assert mock_tracer.start_span.call_count == 2
        assert player._poll_degraded_span is mock_poll_span
        assert player._led_degraded_span is mock_led_span

    @patch("services.game_coordinator.games.base.tracer")
    def test_healthy_game_no_spans(self, mock_tracer):
        """Healthy data should create no child spans."""
        game = _make_game()
        player = _make_player()

        for _ in range(10):
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

        # Health spans should be closed
        mock_poll_span.set_attribute.assert_any_call("health.total_poll_drops", 42)
        mock_poll_span.set_attribute.assert_any_call("health.total_poll_errors", 5)
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

        game._finalize_player_health(player)

        mock_span.set_attribute.assert_any_call("health.total_poll_drops", 10)
        mock_span.set_attribute.assert_any_call("health.total_poll_errors", 0)
        mock_span.set_attribute.assert_any_call("health.total_led_failures", 0)
        mock_span.set_attribute.assert_any_call("health.had_issues", True)

    def test_summary_attributes_healthy(self):
        """health.had_issues should be False when no issues."""
        game = _make_game()

        mock_span = MagicMock()
        player = Player(serial="S1")
        player.span = mock_span

        game._finalize_player_health(player)

        mock_span.set_attribute.assert_any_call("health.had_issues", False)

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
