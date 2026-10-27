"""One write in flight per gameplay stream call (UC-030 BR-006, UC-040 BR-026, #2096).

grpc.aio allows a single outstanding write per call: a second ``write`` that starts while
one is pending fails with GRPC_CALL_ERROR_TOO_MANY_OPERATIONS and ends the whole call with
INTERNAL. The frame path (warning, kill), the intervention handlers, the hold feedback task
and the end-of-game effects all write on the game's ``StreamGameplayData`` call, so every
write goes through ``serialized_write``: one ``asyncio.Lock`` per call object, in call order.

A write is never cancelled once it started: cancelling a grpc.aio ``write`` cancels the whole
call, which would cost every later effect of the game (the end-of-game SHIELD 0, the winner
rainbow). The write runs as its own task behind ``asyncio.shield``; a cancel of the caller
propagates at once, and the lock is released by the write task itself when it ends, so the
next write still waits for it.

An uncontended lock is acquired without suspending, so the frame path pays nothing while
no other write is in flight; when one is, it waits for that write, as long as its own
write would have taken.
"""

import asyncio
import logging
import weakref

logger = logging.getLogger(__name__)

_LOCKS: "weakref.WeakKeyDictionary[object, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _lock_for(stream: object) -> asyncio.Lock:
    lock = _LOCKS.get(stream)
    if lock is None:
        lock = _LOCKS[stream] = asyncio.Lock()
    return lock


def _release_when_done(lock: asyncio.Lock, caller: asyncio.Task | None):
    def _done(task: asyncio.Future) -> None:
        lock.release()
        if task.cancelled():
            return
        error = task.exception()  # retrieved: a caller still waiting gets it through the shield
        if error is not None and caller is not None and (caller.cancelled() or caller.cancelling()):
            # The caller was cancelled while this write ran on: nobody sees the failure but the log.
            logger.debug(f"A gameplay write left running by a cancelled caller failed: {error!r}")

    return _done


async def serialized_write(stream, message) -> None:
    """Write ``message`` on ``stream`` once no other write on the same call is in flight."""
    lock = _lock_for(stream)
    await lock.acquire()
    try:
        # Eager start: the write runs at once up to its first real suspension, so a write
        # that completes without suspending costs no extra loop turn (the frame path's order
        # of events stays as with a plain await).
        write = asyncio.Task(stream.write(message), loop=asyncio.get_running_loop(), eager_start=True)
    except BaseException:
        lock.release()
        raise
    if write.done():
        lock.release()
        write.result()
        return
    write.add_done_callback(_release_when_done(lock, asyncio.current_task()))
    await asyncio.shield(write)
