"""The game clock: the one time source for game deadlines and durations (UC-030 BR-010).

Grace, warning, shield charge and hold, toughness, rubberband, soft penalty, respawn,
round and music deadlines are all compared against :func:`now`. It reads
``time.monotonic()``, so a wall-clock step (NTP or a late RTC sync on a Pi without a
hardware clock) neither ends nor extends a running deadline.

The value is anchored to the wall clock once, at import, so it reads like epoch
seconds and stays comparable with ``time.time()`` until the wall clock steps. It is
NOT a wall-clock timestamp: a value that leaves the process as a timestamp (an
event's ``until_ms`` or ``expires_ms``, compared with the browser clock) goes through
:func:`to_wall`.

Callers use ``game_clock.now()`` (attribute lookup), so a test can patch
``game_clock.now``. Never patch ``time.monotonic`` itself: the asyncio loop uses it.
"""

import time

_WALL_ANCHOR = time.time() - time.monotonic()


def now() -> float:
    """Seconds on the monotonic game clock (anchored to the wall clock at import)."""
    return time.monotonic() + _WALL_ANCHOR


def to_wall(instant: float) -> float:
    """Wall-clock epoch seconds for a game-clock ``instant``, for timestamps sent out."""
    return time.time() + (instant - now())
