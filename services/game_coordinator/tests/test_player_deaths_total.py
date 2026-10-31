"""
Tests for the game_player_deaths_total counter (UC-030 step 9, telemetry §4.4).

Every death through the shared _kill_player path increments the aggregate
counter game_player_deaths_total{mode, game_kind} exactly once. It is a
cumulative counter, so it carries no serial / game_id (unbounded series);
per-game, per-player death order is game_player_elimination_order.
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

from conftest import EventCollector, MockControllerManagerService, MockGameplayStream

from services.game_coordinator.games.base import GameState
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.nonstop_joust import NonstopJoustGame

DEATHS_METRIC = "services.game_coordinator.metrics.player_deaths_total"


async def _make_game(game_cls, game_id="test_deaths_001"):
    mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={}, max_duration=10.0)
    game = game_cls(
        controller_manager_client=mock_cm,
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id=game_id,
    )
    await game._initialize_players_impl(mock_cm.controllers)
    game.gameplay_stream = MockGameplayStream()
    return game


@pytest_asyncio.fixture
async def ffa_game():
    return await _make_game(FFAGame)


def _yield_in_play_sound(game):
    """A real audio RPC suspends the coroutine; model that."""

    async def yielding_play_sound(*_args, **_kwargs):
        await asyncio.sleep(0)

    game._play_sound = yielding_play_sound


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.uc("UC-035", br=["BR-002"])
@pytest.mark.asyncio
async def test_kill_increments_deaths_total_once(ffa_game):
    serial = next(iter(ffa_game.players))

    with patch(DEATHS_METRIC) as mock_metric:
        await ffa_game._kill_player(serial, accel_mag=5.0)

    mock_metric.labels.assert_called_once_with(mode="FFA", game_kind="primary")
    mock_metric.labels.return_value.inc.assert_called_once_with()


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.uc("UC-035", br=["BR-002"])
@pytest.mark.asyncio
async def test_shadow_game_death_labelled_shadow(ffa_game):
    ffa_game.game_kind = "shadow"
    serial = next(iter(ffa_game.players))

    with patch(DEATHS_METRIC) as mock_metric:
        await ffa_game._kill_player(serial, accel_mag=5.0)

    mock_metric.labels.assert_called_once_with(mode="FFA", game_kind="shadow")


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
    _yield_in_play_sound(ffa_game)

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

    mock_metric.labels.return_value.inc.assert_called_once_with()


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_each_player_death_counted(ffa_game):
    with patch(DEATHS_METRIC) as mock_metric:
        for serial in list(ffa_game.players)[:3]:
            await ffa_game._kill_player(serial, accel_mag=5.0)

    assert mock_metric.labels.return_value.inc.call_count == 3


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_respawn_mode_counts_every_death():
    """Nonstop Joust: kill -> respawn -> kill is two deaths."""
    game = await _make_game(NonstopJoustGame)
    game.state = GameState.RUNNING  # NonStop refuses a respawn outside RUNNING (UC-026 BR-003)
    serial = next(iter(game.players))

    with patch(DEATHS_METRIC) as mock_metric:
        await game._kill_player(serial, accel_mag=5.0)
        await game._respawn_player(serial)
        await game._kill_player(serial, accel_mag=5.0)

    mock_metric.labels.assert_called_with(mode="Nonstop Joust", game_kind="primary")
    assert mock_metric.labels.return_value.inc.call_count == 2


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_guard_released_when_mode_impl_raises(ffa_game):
    serial = next(iter(ffa_game.players))

    async def failing_impl(*_args, **_kwargs):
        raise RuntimeError("mode impl failed")

    ffa_game._kill_player_impl = failing_impl

    with pytest.raises(RuntimeError):
        await ffa_game._kill_player(serial, accel_mag=5.0)

    assert serial not in ffa_game._kills_in_progress


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_guard_released_when_kill_is_cancelled(ffa_game):
    """A cancelled kill (e.g. game teardown) must not block a later kill of that player."""
    serial = next(iter(ffa_game.players))
    never = asyncio.Event()

    async def hanging_play_sound(*_args, **_kwargs):
        await never.wait()

    ffa_game._play_sound = hanging_play_sound
    task = asyncio.create_task(ffa_game._kill_player(serial, accel_mag=5.0))
    await asyncio.sleep(0)
    assert serial in ffa_game._kills_in_progress

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert serial not in ffa_game._kills_in_progress
    assert ffa_game.players[serial].alive
