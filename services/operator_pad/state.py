"""Menu status reads for the operator pad (services.md §3.1 `GetMenuStatus`, UC-015 A5).

The pad reads a fresh status at every key press and release; it never caches one across
gestures. A failed read is None: the caller sends nothing and never queues (UC-015 BR-010),
except the K3 hold, which sends the stop (BR-013).
"""

from __future__ import annotations

import logging

from proto import menu_pb2

logger = logging.getLogger(__name__)

STATUS_TIMEOUT_SECONDS = 0.5


# UC-013 / UC-015 BR-004 ladder; an empty variant is a flag the menu could not resolve.
# `probe` and `shadow_experimental` are off the ladder, so they read as unknown (BR-013, fail closed).
AGENT_LEVELS = frozenset({"none", "ambient", "standard", "full"})

# `agent_enabled_state` of a resolved kill switch and `agent_interventions_enabled_state` of a resolved
# write gate; `unknown`, and the empty value of a menu older than #2101 / #2093, are not (services.md §3.1).
ENABLED_STATES = frozenset({"on", "off"})


def is_known(status: menu_pb2.GetMenuStatusResponse | None) -> bool:
    """UC-015 BR-013: a status that was read and whose agent flags resolved.

    `enabled` and the write gate `interventions_enabled` (each state `on` or `off`) and the level
    (on the ladder) must all have resolved.
    """
    return (
        status is not None
        and status.agent_enabled_state in ENABLED_STATES
        and status.agent_interventions_enabled_state in ENABLED_STATES
        and status.agent_level in AGENT_LEVELS
    )


def is_stopped(status: menu_pb2.GetMenuStatusResponse) -> bool:
    """UC-015 BR-002: stopped when `enabled` is off, the level is `none` or the write gate is off (#2093)."""
    return (
        (not status.agent_enabled) or status.agent_level == "none" or status.agent_interventions_enabled_state == "off"
    )


class MenuStatusReader:
    def __init__(self, menu_stub, timeout: float = STATUS_TIMEOUT_SECONDS) -> None:
        self._stub = menu_stub
        self._timeout = timeout

    async def fetch(self) -> menu_pb2.GetMenuStatusResponse | None:
        try:
            return await self._stub.GetMenuStatus(menu_pb2.GetMenuStatusRequest(), timeout=self._timeout)
        except Exception as e:  # grpc.aio.AioRpcError, timeouts, a closed channel
            logger.warning(f"GetMenuStatus failed: {e}")
            return None
