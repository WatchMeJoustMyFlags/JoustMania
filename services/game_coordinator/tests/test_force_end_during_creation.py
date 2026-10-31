"""A force-end while the session still builds the game leaves no orphan game (#2025 S1, UC-027 step 3).

``GameSession.force_end`` runs on the servicer thread; the session builds the game
on its own thread. A force-end that lands before ``current_game`` is set finds no
game to stop: it joins, sets ENDED and publishes ``game_force_ended``. The session
must then not run the game it is building, or that game plays on while the
coordinator reports it ended.
"""

import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from proto import game_coordinator_pb2


def _force_end_from_another_thread(session) -> tuple[bool, str]:
    """Run the real ``GameSession.force_end`` on its own loop and thread, like the servicer."""
    result: list = []
    thread = threading.Thread(target=lambda: result.append(asyncio.run(session.force_end("operator_stop"))))
    thread.start()
    thread.join(timeout=5)
    assert result, "force_end did not finish"
    return result[0]


def _started_session():
    from test_game_session import _make_session

    session = _make_session()
    session.game_running = True  # start_thread sets this
    session.game_state = game_coordinator_pb2.GameState.STARTING  # StartGame sets this
    return session


def _force_ended_events(session):
    return [c for c in session.event_bus.publish.await_args_list if c.args[0] == "game_force_ended"]


@pytest.mark.asyncio
@pytest.mark.uc("UC-027", flow="main", steps=[3])
@patch("services.game_coordinator.game_session.await_flag_providers_ready", new=AsyncMock(return_value=[]))
@patch("services.game_coordinator.game_session.GameFactory")
@patch("services.game_coordinator.game_session.tracer")
async def test_force_end_while_the_game_is_built_runs_no_game(mock_tracer_mod, mock_factory):
    """The force-end lands between the readiness check and ``current_game``: the game never runs."""
    from test_game_session import _tracer_mock

    session = _started_session()
    mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
    game = MagicMock()
    game.run = AsyncMock()
    outcomes = []

    def _create(**_kwargs):
        outcomes.append(_force_end_from_another_thread(session))
        return game

    mock_factory.create_game.side_effect = _create

    await session._run_game_loop_async()

    assert outcomes == [(True, "")]
    assert len(_force_ended_events(session)) == 1
    game.run.assert_not_awaited()
    assert session.current_game is None
    assert session.game_state == game_coordinator_pb2.GameState.ENDED


@pytest.mark.asyncio
@pytest.mark.uc("UC-027", flow="main", steps=[3])
@patch("services.game_coordinator.game_session.await_flag_providers_ready", new=AsyncMock(return_value=[]))
@patch("services.game_coordinator.game_session.GameFactory")
@patch("services.game_coordinator.game_session.tracer")
async def test_force_end_while_clients_connect_creates_no_game(mock_tracer_mod, mock_factory):
    """The force-end lands while the session connects its clients: no game is built or run."""
    from test_game_session import _tracer_mock

    session = _started_session()
    mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
    game = MagicMock()
    game.run = AsyncMock()
    mock_factory.create_game.return_value = game
    outcomes = []

    async def _connect():
        outcomes.append(_force_end_from_another_thread(session))

    session.clients.connect = AsyncMock(side_effect=_connect)

    await session._run_game_loop_async()

    assert outcomes == [(True, "")]
    game.run.assert_not_awaited()
    assert session.current_game is None
    assert session.game_state == game_coordinator_pb2.GameState.ENDED
