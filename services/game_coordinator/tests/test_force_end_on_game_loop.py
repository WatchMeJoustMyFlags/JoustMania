"""A force-end is atomic with the kill path (UC-027 step 3, UC-030 step 9; #2149).

``ForceEndGame`` arrives on the servicer thread, the game runs on its own thread and
event loop. Before #2149 the session called the mode's ``force_end`` from the
servicer thread, so it could land between the kill gate's second check and
``_count_elimination``: that kill then counted after the force-end. The session now
applies the mode's ``force_end`` on the game's loop, so it lands before a kill's gate
check (the kill is declined) or after the kill's bookkeeping, never in between. When
the loop is not running (stopped, closed, or the game thread is gone) it applies it
directly, and a loop that does not take it within the UC-027 wait gets it directly.
"""

import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_game_session import _make_session  # noqa: E402
from test_kill_after_latch import _game, _stub_mode_handler  # noqa: E402

from proto import game_coordinator_pb2  # noqa: E402
from services.game_coordinator import game_session as game_session_mod  # noqa: E402
from services.game_coordinator.games.base import BaseGameMode  # noqa: E402
from services.game_coordinator.games.ffa import FFAGame  # noqa: E402

BASE_METRICS = "services.game_coordinator.games.base.metrics"


class _SignallingLoop(asyncio.SelectorEventLoop):
    """A game loop that reports every callback another thread hands it."""

    def __init__(self) -> None:
        super().__init__()
        self.on_threadsafe = None

    def call_soon_threadsafe(self, callback, *args, context=None):
        handle = super().call_soon_threadsafe(callback, *args, context=context)
        if self.on_threadsafe is not None:
            self.on_threadsafe()
        return handle


class _GameThread:
    """A real game thread running ``loop`` until ``stop()``."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.thread = threading.Thread(target=loop.run_forever, daemon=True)
        self.thread.start()

    async def run(self, coro):
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, self.loop))

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5.0)
        self.loop.close()


def _running_session(game, loop):
    session = _make_session()
    session.game_state = game_coordinator_pb2.GameState.RUNNING
    session.game_running = True
    session.current_game = game
    session.game_loop = loop
    return session


@pytest.mark.uc("UC-027", flow="main", steps=[3])
@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_force_end_landing_between_the_gate_and_the_count_does_not_count_after_it():
    """The force-end is requested right after the kill's second gate check; it takes
    effect only after the kill's bookkeeping, so no elimination counts once the game
    is force-ended, and the next kill is declined."""
    game, _events = _game(FFAGame, ("p1", "p2", "p3"))
    _stub_mode_handler(game)
    loop = _SignallingLoop()
    game_thread = _GameThread(loop)
    session = _running_session(game, loop)
    servicer_loop = asyncio.get_running_loop()

    # Set once the servicer has handed the force-end over: applied on its own
    # thread (the old way) or queued onto the game loop (the fix).
    dispatched = threading.Event()
    real_force_end = game.force_end

    def force_end(reason):
        real_force_end(reason)
        dispatched.set()

    game.force_end = force_end

    counted_while_force_ended: list[bool] = []
    real_count = game._count_elimination

    def count(serial):
        counted_while_force_ended.append(game.force_ended)
        real_count(serial)

    game._count_elimination = count

    force_end_calls = []
    gate_checks = []
    real_gate = game._kill_gate_open

    def gate(serial):
        is_open = real_gate(serial)
        gate_checks.append(is_open)
        if len(gate_checks) == 2 and is_open:
            # The injected hook: the force-end lands here, past the second check.
            loop.on_threadsafe = dispatched.set
            force_end_calls.append(asyncio.run_coroutine_threadsafe(session.force_end("operator"), servicer_loop))
            assert dispatched.wait(5.0)
            loop.on_threadsafe = None
        return is_open

    game._kill_gate_open = gate

    try:
        with patch(BASE_METRICS):
            async with asyncio.timeout(5.0):
                assert await game_thread.run(game._kill_player("p1", 3.0)) is True
                assert await asyncio.wrap_future(force_end_calls[0]) == (True, "")
                assert await game_thread.run(game._kill_player("p2", 3.0)) is False
    finally:
        game_thread.stop()

    assert counted_while_force_ended == [False]
    assert game.force_ended is True
    assert game.players["p2"].alive is True
    assert session.game_state == game_coordinator_pb2.GameState.ENDED


@pytest.mark.uc("UC-027", flow="main", steps=[3])
@pytest.mark.asyncio
async def test_force_end_runs_the_mode_force_end_on_the_game_thread():
    game = create_autospec(BaseGameMode, instance=True)
    ran_on = []
    game.force_end.side_effect = lambda _reason: ran_on.append(threading.current_thread())
    loop = asyncio.new_event_loop()
    game_thread = _GameThread(loop)
    session = _running_session(game, loop)

    try:
        async with asyncio.timeout(5.0):
            assert await session.force_end("operator") == (True, "")
    finally:
        game_thread.stop()

    game.force_end.assert_called_once_with("operator")
    assert ran_on == [game_thread.thread]


@pytest.mark.uc("UC-027", flow="A4", br=["BR-003"])
@pytest.mark.asyncio
async def test_a_mode_force_end_raising_on_the_game_loop_reaches_the_caller_and_can_retry():
    game = create_autospec(BaseGameMode, instance=True)
    game.force_end.side_effect = [RuntimeError("mode failed"), None]
    loop = asyncio.new_event_loop()
    game_thread = _GameThread(loop)
    session = _running_session(game, loop)

    try:
        async with asyncio.timeout(5.0):
            with pytest.raises(RuntimeError, match="mode failed"):
                await session.force_end("operator")
            assert await session.force_end("operator") == (True, "")
    finally:
        game_thread.stop()

    assert game.force_end.call_count == 2


@pytest.mark.uc("UC-027", flow="main", steps=[3])
@pytest.mark.parametrize("closed", [False, True], ids=["stopped", "closed"])
@pytest.mark.asyncio
async def test_force_end_when_the_game_loop_has_stopped_applies_at_once(closed):
    """The game thread's loop is no longer running: the force-end is applied directly,
    without waiting for a loop that will never take it."""
    game = create_autospec(BaseGameMode, instance=True)
    loop = asyncio.new_event_loop()
    if closed:
        loop.close()
    session = _running_session(game, loop)

    try:
        async with asyncio.timeout(1.0):
            assert await session.force_end("operator") == (True, "")
    finally:
        loop.close()

    game.force_end.assert_called_once_with("operator")
    assert session.game_state == game_coordinator_pb2.GameState.ENDED


@pytest.mark.uc("UC-027", flow="main", steps=[3])
@pytest.mark.asyncio
async def test_force_end_on_a_loop_that_never_takes_it_applies_once_within_the_wait():
    """Shutdown / stuck loop: the game loop does not run the queued force-end within
    the UC-027 wait (it is blocked, or tears down with the callback still queued).
    The session applies it directly, and the queued one, if the loop ever runs it,
    does not apply it a second time."""
    game = create_autospec(BaseGameMode, instance=True)
    loop = asyncio.new_event_loop()
    game_thread = _GameThread(loop)
    session = _running_session(game, loop)
    blocked = threading.Event()
    release = threading.Event()

    def block_loop():
        blocked.set()
        release.wait(5.0)

    loop.call_soon_threadsafe(block_loop)
    assert blocked.wait(5.0)

    try:
        with patch.object(game_session_mod, "FORCE_END_WAIT_S", 0.2):
            async with asyncio.timeout(2.0):
                assert await session.force_end("operator") == (True, "")
        game.force_end.assert_called_once_with("operator")
    finally:
        release.set()
        game_thread.stop()

    game.force_end.assert_called_once_with("operator")
    assert session.game_state == game_coordinator_pb2.GameState.ENDED
