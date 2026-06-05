"""
Tests for the game_player_elimination_order metric added in #730 / #722 §7.

Verifies that _kill_player assigns ascending elimination indices (1 = first out)
keyed on (serial, game_id), driven by the game's internal _elimination_count.
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

from services.game_coordinator.games.ffa import FFAGame


class MockGameplayStream:
    """Minimal mock for gameplay stream writes."""

    async def write(self, message):
        pass


@pytest_asyncio.fixture
async def ffa_game():
    """FFA game with 4 initialized players and a mock gameplay stream."""
    mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={}, max_duration=10.0)
    collector = EventCollector()
    game = FFAGame(
        controller_manager_client=mock_cm,
        event_publisher=collector.publish,
        audio_client=None,
        game_id="test_elim_001",
    )
    await game._initialize_players_impl(mock_cm.controllers)
    game.gameplay_stream = MockGameplayStream()
    return game


@pytest.mark.asyncio
async def test_first_death_is_order_one(ffa_game):
    game = ffa_game
    serials = list(game.players.keys())

    with patch("services.game_coordinator.metrics.player_elimination_order") as mock_metric:
        await game._kill_player(serials[0], accel_mag=5.0)

    mock_metric.labels.assert_called_with(serial=serials[0], game_id="test_elim_001")
    mock_metric.labels.return_value.set.assert_called_with(1)
    assert game._elimination_count == 1


@pytest.mark.asyncio
async def test_elimination_order_increments(ffa_game):
    game = ffa_game
    serials = list(game.players.keys())

    set_values = []
    with patch("services.game_coordinator.metrics.player_elimination_order") as mock_metric:
        mock_metric.labels.return_value.set.side_effect = lambda v: set_values.append(v)
        for serial in serials[:3]:
            await game._kill_player(serial, accel_mag=5.0)

    assert set_values == [1, 2, 3]
    assert game._elimination_count == 3


@pytest.mark.asyncio
async def test_already_dead_player_not_recounted(ffa_game):
    game = ffa_game
    serials = list(game.players.keys())

    with patch("services.game_coordinator.metrics.player_elimination_order"):
        await game._kill_player(serials[0], accel_mag=5.0)
        # Killing the same (now-dead) player again is a no-op
        await game._kill_player(serials[0], accel_mag=5.0)

    assert game._elimination_count == 1
