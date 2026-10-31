"""
The unlabeled ``players_alive`` aggregate equals the live count after a death (#1943).

``BaseGameMode._run_kill`` used to compute it before the mode's ``_kill_player_impl``
flipped ``alive``, so every death left the gauge one too high.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

test_dir = Path(__file__).parent
project_root = test_dir.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService, async_noop

from services.game_coordinator.games.base import GameState
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.nonstop_joust import NonstopJoustGame
from services.game_coordinator.games.teams import SimpleTeamsGame
from services.game_coordinator.games.traitor import TraitorGame
from services.game_coordinator.games.werewolf import WerewolfGame
from services.game_coordinator.games.zombie import ZombieGame

AGG = "services.game_coordinator.metrics.players_alive"


class _Stream:
    async def write(self, message):
        pass


async def _make_game(game_cls):
    mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={}, max_duration=10.0)
    game = game_cls(
        controller_manager_client=mock_cm,
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id=f"test_alive_agg_{game_cls.__name__}",
    )
    await game._initialize_players_impl(mock_cm.controllers)
    game.gameplay_stream = _Stream()
    game.running = True
    game._play_sound = async_noop
    return game


def _live(game):
    return len([p for p in game.players.values() if p.alive])


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.parametrize("game_cls", [FFAGame, SimpleTeamsGame, TraitorGame, WerewolfGame, NonstopJoustGame])
@pytest.mark.asyncio
async def test_aggregate_equals_live_count_after_death(game_cls):
    game = await _make_game(game_cls)
    serial = next(iter(game.players))

    with patch(AGG) as agg:
        await game._kill_player(serial, accel_mag=5.0)

    agg.set.assert_called_with(_live(game))
    await game.cleanup()  # NonStop leaves a pending respawn task


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_zombie_death_lowers_aggregate_conversion_does_not():
    game = await _make_game(ZombieGame)
    human = next(s for s, p in game.players.items() if not p.is_zombie)
    zombie = next((s for s, p in game.players.items() if p.is_zombie), None)
    if zombie is None:
        zombie = next(s for s in game.players if s != human)
        game.players[zombie].is_zombie = True

    with patch(AGG) as agg, patch("services.game_coordinator.game_clock.sleep", async_noop):
        await game._kill_player(human, accel_mag=5.0)  # conversion: stays alive
        assert game.players[human].alive
        agg.set.assert_called_with(4)
        await game._kill_player(zombie, accel_mag=5.0)  # zombie death
        assert not game.players[zombie].alive
        agg.set.assert_called_with(3)
    await game.cleanup()


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_nonstop_respawn_restores_aggregate_and_gauge():
    game = await _make_game(NonstopJoustGame)
    serial = next(iter(game.players))
    game.state = GameState.RUNNING

    with patch(AGG) as agg, patch("services.game_coordinator.metrics.player_alive") as gauge:
        await game._kill_player(serial, accel_mag=5.0)
        assert agg.set.call_args_list[-1].args == (3,)
        assert await game._respawn_player(serial)
        assert [c.args for c in agg.set.call_args_list] == [(3,), (4,)]
        gauge.labels.assert_called_with(serial=serial, game_id=game.game_id)
        assert gauge.labels.return_value.set.call_args_list[-1].args == (1,)
    await game.cleanup()


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_generic_revive_restores_aggregate():
    game = await _make_game(FFAGame)
    serial = next(iter(game.players))
    game.state = GameState.RUNNING

    with patch(AGG) as agg:
        await game._kill_player(serial, accel_mag=5.0)
        await game.revive_player(serial)
        assert [c.args for c in agg.set.call_args_list] == [(3,), (4,)]
    await game.cleanup()


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_kill_finishing_after_stop_leaves_end_of_game_aggregate():
    game = await _make_game(FFAGame)
    serial = next(iter(game.players))
    game.running = False

    with patch(AGG) as agg:
        await game._kill_player(serial, accel_mag=5.0)
    agg.set.assert_not_called()
    await game.cleanup()
