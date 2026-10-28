"""A failed game publishes ``game_error`` once, in base-run and override-run modes (#2025 S2).

The base ``run()`` publishes ``game_error`` before it re-raises (a game run without a
session still reports its error); the session publishes it for a mode whose own
``run()`` raises. Through a session the bus carries one ``game_error`` either way
(UC-020 Post-F-1: "System publishes ``game_error`` and ends the session").
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from lib.types import GameEvent
from proto import game_coordinator_pb2
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.fight_club import FightClubGame
from services.game_coordinator.tests.conftest import MockControllerManagerService


@pytest.mark.asyncio
@pytest.mark.uc("UC-020", cond=["Post-F-1"])
@pytest.mark.uc("UC-025", cond=["Post-F-1"])
@pytest.mark.parametrize("game_cls", [FFAGame, FightClubGame], ids=["base-run FFA", "override-run FightClub"])
@patch("services.game_coordinator.game_session.await_flag_providers_ready", new=AsyncMock(return_value=[]))
@patch("services.game_coordinator.game_session.GameFactory")
@patch("services.game_coordinator.game_session.tracer")
async def test_a_game_that_cannot_start_publishes_game_error_once(mock_tracer_mod, mock_factory, game_cls):
    """One player: run() raises; the session's bus sees exactly one game_error."""
    from test_game_session import _make_session, _tracer_mock

    session = _make_session()
    session.game_running = True
    mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span

    def _create(**kwargs):
        return game_cls(
            controller_manager_client=MockControllerManagerService(num_controllers=1, infinite=True),
            event_publisher=kwargs["event_publisher"],
            audio_client=None,
            game_id=kwargs["game_id"],
            initial_players=[game_coordinator_pb2.Player(serial="mock_controller_0")],
        )

    mock_factory.create_game.side_effect = _create

    await session._run_game_loop_async()

    errors = [c for c in session.event_bus.publish.await_args_list if c.args[0] == GameEvent.GAME_ERROR]
    assert len(errors) == 1, f"game_error published {len(errors)} times"
    assert "at least 2 players" in errors[0].args[1]["error"]
    assert session.game_state == game_coordinator_pb2.GameState.ENDED
