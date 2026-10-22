"""End-of-game effects on a gameplay stream the controller manager has already ended (#1956).

After the win the controller manager can end the gameplay call before teardown. grpc.aio
rejects a write on a finished call (``InvalidStateError``), and before #1956 the winner
rainbow write raised out of ``_end_game_impl`` so a won game ended as ``game_error``. Every
mode now skips the end-of-game effect with a warning and keeps its outcome
(UC-020 BR-003, UC-030 BR-006). NonStop got the guard first in #1955.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import grpc
import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from lib.types import GameEvent, Games
from proto import controller_manager_pb2
from services.game_coordinator.game_factory import _GAME_CLASSES
from services.game_coordinator.games.base import GameState
from services.game_coordinator.games.tournament import TournamentState

ALL_MODES = list(_GAME_CLASSES)


class _EndedStream:
    """A grpc.aio gameplay call the server has ended: ``done()`` and every write raises.

    ``done=False`` models the race: the call ends between the check and the write.
    """

    def __init__(self, done: bool = True, error: BaseException | None = None):
        self._done = done
        self.error = error or asyncio.InvalidStateError("RPC already finished")
        self.attempts = 0

    def done(self) -> bool:
        return self._done

    async def write(self, _message):
        self.attempts += 1
        raise self.error


def _make_winner(game, mode: Games) -> None:
    """Put the game in a state where ``_end_game_impl`` declares a winner (and writes the rainbow)."""
    serials = list(game.players)
    if mode in (Games.JoustFFA, Games.FightClub):
        for serial in serials[1:]:
            game.players[serial].alive = False
        game.players[serials[0]].score = 5
    elif mode in (Games.JoustTeams, Games.JoustRandomTeams):
        winning_team = game.players[serials[0]].team
        for player in game.players.values():
            player.alive = player.team == winning_team
    elif mode == Games.Traitor:
        winning_team = game.players[serials[0]].secret_team
        for player in game.players.values():
            player.alive = player.secret_team == winning_team
    elif mode == Games.Swapper:
        for player in game.players.values():
            player.team = 0
    elif mode == Games.Werewolf:
        for serial in game.werewolf_serials:
            game.players[serial].alive = False
    elif mode == Games.Zombies:
        game.time_remaining = 0
    elif mode == Games.Tournament:
        game.players[serials[0]].tournament_state = TournamentState.CHAMPION
        for serial in serials[1:]:
            game.players[serial].tournament_state = TournamentState.ELIMINATED
    elif mode == Games.NonStop:
        game._time_limit_reached = True
        game.players[serials[0]].score = 5
    else:  # pragma: no cover - a new mode must say how it wins
        raise AssertionError(f"no winner setup for {mode}")


async def _won_game(mode: Games, stream):
    """A game of ``mode`` past its win latch, its gameplay stream replaced by ``stream``."""
    events = EventCollector()
    cm = MockControllerManagerService(num_controllers=4)
    game = _GAME_CLASSES[mode](
        controller_manager_client=cm,
        event_publisher=events.publish,
        audio_client=None,
        game_id=f"test_end_effect_{mode.name.lower()}",
        initial_players=[type("P", (), {"serial": c.serial})() for c in cm.controllers],
    )
    await game._initialize_players_impl(cm.controllers)
    game.state = GameState.RUNNING
    game.start_time = 100.0
    game.running = True
    game._play_sound = AsyncMock()
    game._wait_for_rainbow_effect = AsyncMock(return_value=True)
    _make_winner(game, mode)
    if mode not in (Games.FightClub, Games.Tournament, Games.NonStop):
        assert await game._check_win_condition()
    game._latch_game_outcome()
    # The loop has ended: no celebration / hold tick waits on a running game.
    game.running = False
    game.gameplay_stream = stream
    return game, events


@pytest.mark.uc("UC-030", br=["BR-006"])
@pytest.mark.parametrize("mode", ALL_MODES, ids=[m.name for m in ALL_MODES])
@pytest.mark.asyncio
async def test_end_effects_skip_a_stream_the_server_ended(mode):
    """Every mode: a finished call gets no write and the end of game completes (#1956)."""
    stream = _EndedStream(done=True)
    game, events = await _won_game(mode, stream)

    with patch("services.game_coordinator.games.nonstop_joust.WINNER_HOLD_SECONDS", 0):
        await game._end_game_impl()

    assert stream.attempts == 0, "an end-of-game effect was written to a finished call"
    assert events.get_events_of_type(GameEvent.GAME_ENDED)
    assert not events.get_events_of_type(GameEvent.GAME_ERROR)


@pytest.mark.uc("UC-030", br=["BR-006"])
@pytest.mark.parametrize("mode", ALL_MODES, ids=[m.name for m in ALL_MODES])
@pytest.mark.asyncio
async def test_end_effects_survive_a_stream_ending_under_the_write(mode):
    """Every mode: the call ends between the check and the write; the write error is swallowed (#1956)."""
    stream = _EndedStream(done=False)
    game, events = await _won_game(mode, stream)

    with patch("services.game_coordinator.games.nonstop_joust.WINNER_HOLD_SECONDS", 0):
        await game._end_game_impl()

    assert stream.attempts >= 1
    assert events.get_events_of_type(GameEvent.GAME_ENDED)


@pytest.mark.uc("UC-020", flow="main", steps=[6], br=["BR-003"])
@pytest.mark.asyncio
async def test_ffa_winner_survives_a_failed_call_under_the_rainbow():
    """FFA: a call that fails under the write (AioRpcError) keeps the winner (#1956)."""
    error = grpc.aio.AioRpcError(grpc.StatusCode.CANCELLED, grpc.aio.Metadata(), grpc.aio.Metadata())
    stream = _EndedStream(done=False, error=error)
    game, events = await _won_game(Games.JoustFFA, stream)

    await game._end_game_impl()

    assert game.game_outcome() == "winner"
    assert events.get_events_of_type(GameEvent.GAME_ENDED)


@pytest.mark.uc("UC-030", br=["BR-006"])
@pytest.mark.asyncio
async def test_a_cancel_of_the_ending_task_still_propagates():
    """Only a closed stream is swallowed: a cancel of the task under the write propagates."""
    stream = _EndedStream(done=False, error=asyncio.CancelledError())
    game, _events = await _won_game(Games.JoustFFA, stream)
    effect = controller_manager_pb2.GameplayStreamControl()

    with pytest.raises(asyncio.CancelledError):
        await game._write_end_effect(effect)


@pytest.mark.uc("UC-030", br=["BR-006"])
@pytest.mark.asyncio
async def test_write_end_effect_writes_to_an_open_call():
    """An open call gets the effect; no stream at all is a no-op."""
    game, _events = await _won_game(Games.JoustFFA, None)
    effect = controller_manager_pb2.GameplayStreamControl()
    assert await game._write_end_effect(effect) is False

    written = []

    class _OpenStream:
        def done(self):
            return False

        async def write(self, message):
            written.append(message)

    game.gameplay_stream = _OpenStream()
    assert await game._write_end_effect(effect) is True
    assert written == [effect]
