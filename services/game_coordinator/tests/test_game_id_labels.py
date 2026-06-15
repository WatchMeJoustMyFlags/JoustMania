"""
Tests for the additive ``game_id`` label on live-session and per-player metrics,
plus ``game.id`` / ``game.kind`` on the player_lifecycle span (#845).

These guarantee the producer side of per-game agent partitioning: every live
signal the agent routes on is stamped with the emitting session's game_id, so
concurrent games never blur into one partition.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from proto import game_coordinator_pb2
from services.game_coordinator import metrics
from services.game_coordinator.games.ffa import FFAGame

GAME_ID = "test_labels_001"


class MockGameplayStream:
    async def write(self, message):
        pass


@pytest_asyncio.fixture
async def ffa_game():
    """FFA game with 4 initialized players and a known game_id."""
    mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={}, max_duration=10.0)
    collector = EventCollector()
    game = FFAGame(
        controller_manager_client=mock_cm,
        event_publisher=collector.publish,
        audio_client=None,
        game_id=GAME_ID,
    )
    await game._initialize_players_impl(mock_cm.controllers)
    game.gameplay_stream = MockGameplayStream()
    return game


class TestPerPlayerMetricLabels:
    @pytest.mark.asyncio
    async def test_kill_player_stamps_alive_with_game_id(self, ffa_game):
        game = ffa_game
        serial = next(iter(game.players))

        with patch("services.game_coordinator.metrics.player_alive") as mock_alive:
            await game._kill_player(serial, accel_mag=5.0)

        mock_alive.labels.assert_called_with(serial=serial, game_id=GAME_ID)
        mock_alive.labels.return_value.set.assert_called_with(0)

    @pytest.mark.asyncio
    async def test_initialize_marks_alive_with_game_id(self):
        mock_cm = MockControllerManagerService(num_controllers=2, death_schedule={}, max_duration=10.0)
        collector = EventCollector()
        game = FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=collector.publish,
            audio_client=None,
            game_id=GAME_ID,
            initial_players=[game_coordinator_pb2.Player(serial="p1"), game_coordinator_pb2.Player(serial="p2")],
        )

        with patch("services.game_coordinator.metrics.player_alive") as mock_alive:
            # The StartGame-RPC init path sets player_alive=1 per player.
            await game._initialize_players()

        assert mock_alive.labels.call_args_list, "player_alive never labeled during init"
        for call in mock_alive.labels.call_args_list:
            assert call.kwargs["game_id"] == GAME_ID
        mock_alive.labels.return_value.set.assert_called_with(1)


class TestPlayersAliveAggregateIsPrimaryOnly:
    """The unlabeled ``players_alive`` aggregate gauge (the live-dashboard count)
    is written by the PRIMARY session only. With concurrent shadow games (#1018)
    a headless shadow must not stomp the real game's count via this single
    last-writer-wins series."""

    @pytest.mark.asyncio
    async def test_primary_kill_writes_players_alive(self, ffa_game):
        game = ffa_game  # default game_kind == "primary"
        serial = next(iter(game.players))

        with patch("services.game_coordinator.metrics.players_alive") as mock_agg:
            await game._kill_player(serial, accel_mag=5.0)

        mock_agg.set.assert_called()  # primary updates the aggregate

    @pytest.mark.asyncio
    async def test_shadow_kill_does_not_write_players_alive(self, ffa_game):
        game = ffa_game
        game.game_kind = "shadow"
        serial = next(iter(game.players))

        with patch("services.game_coordinator.metrics.players_alive") as mock_agg:
            await game._kill_player(serial, accel_mag=5.0)

        mock_agg.set.assert_not_called()  # shadow must NOT touch the aggregate

    @pytest.mark.asyncio
    async def test_shadow_initialize_does_not_write_players_alive(self):
        mock_cm = MockControllerManagerService(num_controllers=2, death_schedule={}, max_duration=10.0)
        collector = EventCollector()
        game = FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=collector.publish,
            audio_client=None,
            game_id=GAME_ID,
            initial_players=[game_coordinator_pb2.Player(serial="p1"), game_coordinator_pb2.Player(serial="p2")],
        )
        game.game_kind = "shadow"

        with patch("services.game_coordinator.metrics.players_alive") as mock_agg:
            await game._initialize_players()

        mock_agg.set.assert_not_called()


class TestLifecycleSpanGameIdentity:
    def test_lifecycle_span_carries_game_id_and_kind(self, ffa_game):
        game = ffa_game
        game.game_kind = "shadow"
        serial = next(iter(game.players))

        with patch("services.game_coordinator.games.base.tracer") as mock_tracer:
            game._create_player_lifecycle_span(serial)

        _, kwargs = mock_tracer.start_span.call_args
        attrs = kwargs["attributes"]
        assert attrs["game.id"] == GAME_ID
        assert attrs["game.kind"] == "shadow"
        assert attrs["player.serial"] == serial


class TestMetricDeclarationsCarryGameId:
    """The label set on the gauge/counter declarations must include game_id so
    the agent's extractors can read it (contract for PR A)."""

    def test_live_session_metrics_have_game_id(self):
        for metric in (metrics.active_game, metrics.active_players, metrics.game_duration_seconds):
            assert "game_id" in metric.labelnames

    def test_per_player_metrics_have_game_id(self):
        for metric in (
            metrics.player_alive,
            metrics.player_accel_magnitude,
            metrics.player_movement_zone,
            metrics.player_playstyle,
            metrics.player_movement_variance,
            metrics.player_movement_variance_aggregate,
            metrics.player_skill_level,
            metrics.player_peak_accel,
            metrics.player_elimination_order,
            metrics.player_deaths_total,
        ):
            assert "game_id" in metric.labelnames


class TestCleanupRemovesGameIdScopedSeries:
    def test_clear_player_analytics_removes_serial_and_game_id_tuple(self):
        with (
            patch("services.game_coordinator.metrics.player_alive") as mock_alive,
            patch("services.game_coordinator.metrics.player_accel_magnitude") as mock_accel,
            patch("services.game_coordinator.metrics.player_skill_level") as mock_skill,
            patch("services.game_coordinator.metrics.player_movement_variance_aggregate") as mock_var_agg,
        ):
            metrics.clear_player_analytics("AA:BB:CC", game_id=GAME_ID)

        mock_alive.remove.assert_called_once_with("AA:BB:CC", GAME_ID)
        mock_accel.remove.assert_called_once_with("AA:BB:CC", GAME_ID)
        mock_skill.remove.assert_called_once_with("AA:BB:CC", GAME_ID)
        mock_var_agg.remove.assert_called_once_with("AA:BB:CC", GAME_ID)
