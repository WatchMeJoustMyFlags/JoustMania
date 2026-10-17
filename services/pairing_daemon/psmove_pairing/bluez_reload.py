"""Whether and when pairing restarts bluetoothd (UC-001 BR-005, #1446).

A bluetoothd restart powers every adapter off and drops every connected
controller, so it is governed by the ``pairing.bluez_restart_mode`` flag:

- ``when_idle`` (shipped default, #1466): restart only after a new BlueZ device
  record was written (UC-001 A7, BR-008), and only while the real (primary)
  game is not starting, running or ending. The restart is otherwise left
  pending and retried on each USB poll (under the pairing lock).
- ``never`` (fallback when flagd is unreachable): never restart.
- ``always``: restart after every successful pairing (pre-#1446 behaviour).

A restart ends the bluetoothctl agent session (#632), so the pairing agent is
registered again after every restart.

The primary game's state comes from the game coordinator's ``GetGameState``
(empty ``game_id`` = primary) through envoy's Connect route. The daemon runs on
the host network, where envoy is the one published entry point, so no gRPC
port or proto client is needed. An unreadable state counts as busy (fail closed).
The read ignores HTTP proxy settings: a host ``HTTP_PROXY`` must not intercept it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from collections.abc import Awaitable, Callable

from lib import bluez_dbus

from .bluez_agent import register_pairing_agent
from .config import get_bluez_restart_mode

logger = logging.getLogger("psmove-pairing")

DEFAULT_GAME_STATE_URL = "http://localhost/joustmania.game_coordinator.GameCoordinatorService/GetGameState"
GAME_STATE_TIMEOUT_SECONDS = 2.0
# Players hold connected controllers from the countdown until the results
BUSY_GAME_STATES = frozenset({"STARTING", "RUNNING", "ENDING"})
# Time for bluetoothd to come back and re-read its storage
RESTART_SETTLE_SECONDS = 3.0
# No proxy handler: HTTP_PROXY / ALL_PROXY must not intercept the local envoy route
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch_primary_game_state(url: str) -> str:
    """POST an empty GetGameStateRequest (Connect JSON) and return the primary game's state name."""
    request = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    with _OPENER.open(request, timeout=GAME_STATE_TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed local URL
        body = json.loads(response.read() or b"{}")
    if not body.get("success", False):
        raise RuntimeError(f"GetGameState failed: {body.get('error', 'no success flag')}")
    # proto3 JSON omits default values: a missing state is IDLE
    return body.get("gameInfo", {}).get("state", "IDLE")


async def primary_game_busy() -> bool:
    """True while the real game is starting, running or ending, or when its state is unknown."""
    url = os.getenv("GAME_STATE_URL", DEFAULT_GAME_STATE_URL)
    try:
        state = await asyncio.to_thread(_fetch_primary_game_state, url)
    except Exception as e:
        logger.warning(f"Cannot read the primary game state ({e}); treating it as running")
        return True
    return state in BUSY_GAME_STATES


async def restart_bluetoothd() -> None:
    """Restart bluetooth.service through systemd's D-Bus API, let it settle, re-register the agent.

    The restart ends the bluetoothctl agent session (#632); without a new one a
    ZCM2 has no PIN agent. The agent is registered again even when the restart
    call failed, since bluetoothd may have restarted anyway.
    """
    try:
        await bluez_dbus.restart_systemd_unit("bluetooth.service")
        await asyncio.sleep(RESTART_SETTLE_SECONDS)
    finally:
        await register_pairing_agent()


class BluezReloader:
    """Applies pairing.bluez_restart_mode; holds a deferred restart between polls."""

    def __init__(
        self,
        game_busy: Callable[[], Awaitable[bool]] = primary_game_busy,
        restart: Callable[[], Awaitable[None]] = restart_bluetoothd,
    ):
        self._game_busy = game_busy
        self._restart = restart
        self.pending = False

    async def after_pairing(self, record_written: bool) -> str:
        """Called after a successful pairing; returns the outcome for the span.

        Outcomes: ``skipped`` (nothing to do), ``restarted``, ``deferred``
        (when_idle and a real game is busy), ``failed``.
        """
        mode = get_bluez_restart_mode()
        if mode == "never":
            self.pending = False
            return "skipped"
        if mode == "when_idle" and not record_written and not self.pending:
            return "skipped"
        self.pending = True
        return await self.run_pending()

    async def run_pending(self) -> str:
        """Run a pending restart if the mode allows it now; returns the outcome."""
        if not self.pending:
            return "skipped"
        mode = get_bluez_restart_mode()
        if mode == "never":
            logger.info("Dropping a pending bluetoothd restart: pairing.bluez_restart_mode is never")
            self.pending = False
            return "skipped"
        if mode == "when_idle" and await self._game_busy():
            logger.info("Deferring the bluetoothd restart until the real game has ended")
            return "deferred"
        self.pending = False
        logger.info(f"Restarting bluetoothd (pairing.bluez_restart_mode={mode})")
        try:
            await self._restart()
        except Exception as e:
            logger.error(f"Failed to restart bluetooth service: {e}")
            return "failed"
        return "restarted"
