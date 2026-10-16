"""
Tests for the game_player_deaths_total counter (UC-030 step 9, telemetry §4.4).

The agent's live elimination fallback (services/agent/gamecontext RecordDeathTotal)
reads this counter, so every death through the shared _kill_player path must
increment it exactly once with {mode, serial, game_id}.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from services.game_coordinator.games.ffa import FFAGame

DEATHS_METRIC = "services.game_coordinator.metrics.player_deaths_total"


class MockGameplayStream:
    async def write(self, message):
        pass


@pytest_asyncio.fixture
async def ffa_game():
    mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={}, max_duration=10.0)
    collector = EventCollector()
    game = FFAGame(
        controller_manager_client=mock_cm,
        event_publisher=collector.publish,
        audio_client=None,
        game_id="test_deaths_001",
    )
    await game._initialize_players_impl(mock_cm.controllers)
    game.gameplay_stream = MockGameplayStream()
    return game


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_kill_increments_deaths_total_once(ffa_game):
    serial = next(iter(ffa_game.players))

    with patch(DEATHS_METRIC) as mock_metric:
        await ffa_game._kill_player(serial, accel_mag=5.0)

    mock_metric.labels.assert_called_once_with(mode="FFA", serial=serial, game_id="test_deaths_001")
    mock_metric.labels.return_value.inc.assert_called_once_with()


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_already_dead_player_not_recounted(ffa_game):
    serial = next(iter(ffa_game.players))

    with patch(DEATHS_METRIC) as mock_metric:
        await ffa_game._kill_player(serial, accel_mag=5.0)
        await ffa_game._kill_player(serial, accel_mag=5.0)

    assert mock_metric.labels.return_value.inc.call_count == 1


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_concurrent_kills_of_same_player_count_once(ffa_game):
    """A motion death and a disconnect racing on the same serial count one death.

    _kill_player awaits (death sound) before the mode impl flips player.alive,
    so without a re-entrancy guard both calls would pass the alive check.
    """
    serial = next(iter(ffa_game.players))

    async def yielding_play_sound(*_args, **_kwargs):
        # A real audio RPC suspends the coroutine; model that.
        await asyncio.sleep(0)

    ffa_game._play_sound = yielding_play_sound

    with patch(DEATHS_METRIC) as mock_metric:
        await asyncio.gather(
            ffa_game._kill_player(serial, accel_mag=5.0),
            ffa_game._handle_disconnect(serial),
        )

    assert mock_metric.labels.return_value.inc.call_count == 1
    assert ffa_game._elimination_count == 1
    assert ffa_game.dead_count == 1


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_disconnect_kill_counts_as_death(ffa_game):
    serial = next(iter(ffa_game.players))

    with patch(DEATHS_METRIC) as mock_metric:
        await ffa_game._handle_disconnect(serial)

    mock_metric.labels.assert_called_once_with(mode="FFA", serial=serial, game_id="test_deaths_001")
    mock_metric.labels.return_value.inc.assert_called_once_with()


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_each_player_death_counted(ffa_game):
    serials = list(ffa_game.players)[:3]

    with patch(DEATHS_METRIC) as mock_metric:
        for serial in serials:
            await ffa_game._kill_player(serial, accel_mag=5.0)

    labelled = [c.kwargs["serial"] for c in mock_metric.labels.call_args_list]
    assert labelled == serials
    assert mock_metric.labels.return_value.inc.call_count == 3
