"""One write in flight per gameplay stream call (UC-030 BR-006, UC-040 BR-026, #2096).

grpc.aio allows a single outstanding write per call: a second ``write`` that starts while
one is pending fails with GRPC_CALL_ERROR_TOO_MANY_OPERATIONS and ends the whole call with
INTERNAL. The frame path (warning, kill), the intervention handlers, the hold feedback task
and the end-of-game effects all write on the game's ``StreamGameplayData`` call, so every
write goes through ``serialized_write``: one ``asyncio.Lock`` per call object, in call order.

An uncontended lock is acquired without suspending, so the frame path pays nothing while
no other write is in flight; when one is, it waits for that single write, as long as its
own write would have taken.
"""

import asyncio
import weakref

_LOCKS: "weakref.WeakKeyDictionary[object, asyncio.Lock]" = weakref.WeakKeyDictionary()
# Call objects that cannot be weak-referenced (none known today) fall back to an id map.
_LOCKS_BY_ID: dict[int, tuple[object, asyncio.Lock]] = {}


def _lock_for(stream: object) -> asyncio.Lock:
    try:
        lock = _LOCKS.get(stream)
        if lock is None:
            lock = _LOCKS[stream] = asyncio.Lock()
        return lock
    except TypeError:
        entry = _LOCKS_BY_ID.get(id(stream))
        if entry is None or entry[0] is not stream:
            entry = _LOCKS_BY_ID[id(stream)] = (stream, asyncio.Lock())
        return entry[1]


async def serialized_write(stream, message) -> None:
    """Write ``message`` on ``stream`` once no other write on the same call is in flight."""
    async with _lock_for(stream):
        await stream.write(message)
