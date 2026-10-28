"""NonStop's own gameplay stream is not reconnected (#2025 S5 pin, UC-026 BR-003).

UC-026 BR-003: NonStop opens its own gameplay stream in its game loop and does not
reconnect it (the base loop's stream recovery does not apply). A server-side end
is pinned in ``test_nonstop.py`` (outcome ``completed``, no winner); this pins the
other end: a stream that breaks with a gRPC error ends the game with one
``game_error``, without a new stream and without a winner.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import grpc
import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_nonstop import (  # noqa: E402
    _assert_no_winner,
    _ClosableQueueStream,
    _nonstop_run_game,
    _NonstopControllerManager,
    _record_sounds,
    _stop_producers,
    fast_nonstop,  # noqa: F401 - fixture
)
from test_random_teams_formation import RecordingLog  # noqa: E402

from lib.types import GameEvent  # noqa: E402
from services.game_coordinator.games import nonstop_joust as nonstop_mod  # noqa: E402


class _BreakingStream(_ClosableQueueStream):
    """NonStop's game stream: breaks with UNAVAILABLE after a few frames."""

    def __init__(self, log, break_after: int):
        super().__init__(log)
        self.break_after = break_after

    async def __anext__(self):
        if self.read >= self.break_after:
            raise grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, None, None, "stream broke")
        return await super().__anext__()


class _BreakingGameStreamManager(_NonstopControllerManager):
    def StreamGameplayData(self):  # noqa: N802 - gRPC stub method name
        if not self.streams:
            return super().StreamGameplayData()
        stream = _BreakingStream(self.log, break_after=5)
        self.streams.append(stream)
        return stream


@pytest.mark.uc("UC-026", br=["BR-003"])
@pytest.mark.uc("UC-026", cond=["Post-F-1"])
@pytest.mark.usefixtures("fast_nonstop")
@pytest.mark.asyncio
async def test_a_broken_game_stream_ends_with_one_game_error_and_no_reconnect():
    log = RecordingLog()
    client = _BreakingGameStreamManager(log)
    game = _nonstop_run_game(log, client, time_limit=0)
    sounds = _record_sounds(game)
    try:
        with patch.object(nonstop_mod, "WINNER_HOLD_SECONDS", 30.0), pytest.raises(grpc.aio.AioRpcError):
            await asyncio.wait_for(game.run(), timeout=5)
    finally:
        _stop_producers(client)

    assert len(client.streams) == 2, "NonStop reopened its game stream (UC-026 BR-003: no reconnect)"
    assert len(log.events.get_events_of_type(GameEvent.GAME_ERROR)) == 1
    _assert_no_winner(log, sounds)
