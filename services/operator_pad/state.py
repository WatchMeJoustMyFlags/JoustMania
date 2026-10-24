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
AGENT_LEVELS = frozenset({"none", "ambient", "standard", "full"})


def is_known(status: menu_pb2.GetMenuStatusResponse | None) -> bool:
    """UC-015 BR-013: a status that was read and whose agent flags resolved (level on the ladder)."""
    return status is not None and status.agent_level in AGENT_LEVELS


def is_stopped(status: menu_pb2.GetMenuStatusResponse) -> bool:
    """UC-015 BR-002: the agent is stopped when `enabled` is off or the level is `none`."""
    return (not status.agent_enabled) or status.agent_level == "none"


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
