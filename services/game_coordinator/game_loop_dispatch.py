"""
Run intervention side effects on the owning game's event loop (UC-040 BR-018, #1250).

Each game runs on its own thread with its own asyncio loop (``GameSession``), and
its gRPC clients (audio, controller manager) and gameplay stream are bound to
that loop. The ``InterventionManager`` runs on the servicer's main loop; awaiting
a game's client from there fails with "Future attached to a different loop".

``GameLoopDispatcher`` is the per-session hop: the manager keeps its bookkeeping
(baselines, enforcement chain) on the main loop and hands only the side effect to
the game's dispatcher, which:

- runs it inline when the caller is already on the game's loop (or the session
  has no loop, the legacy single-loop path);
- otherwise schedules it on the game's loop with ``run_coroutine_threadsafe``.
  Each effect starts only after the previous effect of the same game has really
  finished, so one game's effects run one at a time in submission (decision)
  order;
- awaits it from the caller's loop without blocking that loop, for at most
  ``timeout_s``.

An effect is never cancelled once it has started: a handler cancelled midway
could leave the game half-changed (e.g. a death counted but the player still
alive). So when the wait ends badly:

- the effect had not started (closed / stopped loop, or still queued behind a
  slow effect at the deadline): it is skipped for good and the caller gets
  ``GameLoopUnavailableError`` with ``game_loop_unavailable`` /
  ``game_loop_timeout``: NOT applied;
- the effect had started: it is left to finish on the game loop and the caller
  gets ``game_loop_late``: outcome unknown (it may still apply).
"""

import asyncio
import concurrent.futures
import threading
from collections.abc import Awaitable, Callable
from typing import Any

# How long the main loop waits for one side effect on a game loop. Handlers are
# short (a gRPC call or a stream write); a game loop that cannot finish one in
# this window is treated as unavailable rather than stalling evaluation.
DEFAULT_DISPATCH_TIMEOUT_S = 5.0

REASON_LOOP_UNAVAILABLE = "game_loop_unavailable"
REASON_LOOP_TIMEOUT = "game_loop_timeout"
# The effect started but did not finish in time: it keeps running on the game
# loop and may still apply (outcome unknown, not "not applied").
REASON_LOOP_LATE = "game_loop_late"


class GameLoopUnavailableError(Exception):
    """The side effect did not complete on the game's loop.

    ``reason`` is ``REASON_LOOP_UNAVAILABLE`` or ``REASON_LOOP_TIMEOUT`` (not
    applied) or ``REASON_LOOP_LATE`` (started, outcome unknown); it is used as
    the ``block_reason`` / metric ``reason`` label. ``applied_unknown`` is True
    only for ``REASON_LOOP_LATE``.
    """

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason

    @property
    def applied_unknown(self) -> bool:
        return self.reason == REASON_LOOP_LATE


def _running_loop_or_none() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class _EffectState:
    """Start/skip handshake between the caller and the game loop for one effect.

    The game loop sets ``started`` right before calling the effect; the caller
    sets ``skip`` when it gives up on an effect that has not started. Whichever
    takes the lock first decides, so an effect is either run to completion or
    never run at all.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.started = False
        self.skip = False

    def try_start(self) -> bool:
        with self._lock:
            if self.skip:
                return False
            self.started = True
            return True

    def try_skip(self) -> bool:
        """Skip the effect if it has not started. True if it is now skipped."""
        with self._lock:
            if not self.started:
                self.skip = True
            return self.skip


class GameLoopDispatcher:
    """Serialised, timeout-bounded dispatch of coroutines onto one game's loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None, timeout_s: float = DEFAULT_DISPATCH_TIMEOUT_S):
        self.loop = loop
        self._timeout_s = timeout_s
        # Completion of the last effect submitted cross-loop. It is resolved only
        # when that effect has really finished (or was skipped), never when the
        # caller gave up waiting, so the next effect cannot overtake it.
        self._tail: concurrent.futures.Future | None = None
        self._tail_lock = threading.Lock()

    async def run(self, fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        """Run ``await fn(*args)`` on the game's loop and return its result.

        ``fn(*args)`` is only called on the game loop, after the effect has been
        admitted, so a skipped effect never creates its coroutine. Exceptions
        raised by ``fn`` propagate unchanged. See the module docstring for the
        failure outcomes.
        """
        loop = self.loop
        if loop is None or loop is _running_loop_or_none():
            return await fn(*args)
        if loop.is_closed() or not loop.is_running():
            raise GameLoopUnavailableError(REASON_LOOP_UNAVAILABLE, "game loop is closed or not running")

        state = _EffectState()
        done: concurrent.futures.Future = concurrent.futures.Future()
        with self._tail_lock:
            previous = self._tail
            wrapper = self._chained(previous, done, state, fn, args)
            try:
                cfut = asyncio.run_coroutine_threadsafe(wrapper, loop)
            except RuntimeError as e:  # loop closed between the check and the schedule
                wrapper.close()
                raise GameLoopUnavailableError(REASON_LOOP_UNAVAILABLE, str(e)) from e
            self._tail = done

        waiter = asyncio.wrap_future(cfut)
        # The waiter may outlive this call (late effect); never leave its
        # exception unretrieved.
        waiter.add_done_callback(lambda f: f.cancelled() or f.exception())
        try:
            finished, _ = await asyncio.wait({waiter}, timeout=self._timeout_s)
        except asyncio.CancelledError:
            state.try_skip()  # the caller is going away: never start it late
            raise
        if finished:
            if not waiter.cancelled():
                return waiter.result()
            # The game loop cancelled the wrapper (its shutdown cancels all tasks).
            if state.started:
                raise GameLoopUnavailableError(REASON_LOOP_LATE, "game loop stopped while the effect ran")
            raise GameLoopUnavailableError(REASON_LOOP_UNAVAILABLE, "game loop stopped before the effect ran")

        if state.try_skip():
            if loop.is_closed() or not loop.is_running():
                raise GameLoopUnavailableError(REASON_LOOP_UNAVAILABLE, "game loop stopped before the effect ran")
            detail = f"effect did not start within {self._timeout_s}s"
            raise GameLoopUnavailableError(REASON_LOOP_TIMEOUT, detail)
        detail = f"effect started but did not finish within {self._timeout_s}s; it may still apply"
        raise GameLoopUnavailableError(REASON_LOOP_LATE, detail)

    @staticmethod
    async def _chained(
        previous: concurrent.futures.Future | None,
        done: concurrent.futures.Future,
        state: _EffectState,
        fn: Callable[..., Awaitable[Any]],
        args: tuple,
    ) -> Any:
        try:
            if previous is not None and not previous.done():
                # asyncio.wait never raises the previous effect's outcome.
                await asyncio.wait([asyncio.wrap_future(previous)])
            if not state.try_start():
                return None  # the caller gave up before it started: not applied
            return await fn(*args)
        finally:
            done.set_result(None)
