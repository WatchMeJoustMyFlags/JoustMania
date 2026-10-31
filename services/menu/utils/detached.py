"""Fire-and-forget tasks for the menu (#2378, the pattern of #1746 and #2312).

The menu handles every controller's button events one after another on one stream
(UC-010 BR-012), so feedback that only has to be heard (voice lines, beeps) must not be
awaited there: a slow or hung audio service would hold back the next press of every
controller. ``run_detached`` is the one place that starts such a task: the caller's set
keeps a reference until the task ends (an unreferenced task can be garbage-collected
mid-run), and an error is logged instead of being lost or raised into the caller.
"""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)


def run_detached(coro: Coroutine[Any, Any, Any], *, tasks: set[asyncio.Task], what: str) -> asyncio.Task:
    """Start ``coro`` as a task without awaiting it; ``tasks`` holds it until it ends."""
    task = asyncio.get_running_loop().create_task(_logged(coro, what))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


async def _logged(coro: Coroutine[Any, Any, Any], what: str) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"{what} failed: {e}")
