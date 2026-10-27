"""Whether and when pairing restarts bluetoothd (UC-001 BR-005, #1446).

A bluetoothd restart powers every adapter off and drops every connected
controller, so it is governed by the ``pairing.bluez_restart_mode`` flag:

- ``when_idle`` (shipped default, #1466): restart only after a new BlueZ device
  record was written (UC-001 A7, BR-008), and only while the real (primary)
  game is not starting, running or ending. The restart is otherwise left
  pending and retried on each USB poll (under the pairing lock); so is a
  restart that failed, with a growing wait between attempts (#1490).
- ``never`` (fallback when flagd is unreachable): never restart.
- ``always``: restart after every successful pairing (pre-#1446 behaviour).

With ``when_idle`` the restart also waits until no PS Move has been on USB for
``pairing.restart_quiet_period`` seconds (#2091): a host pairing a stack of
controllers one after the other gets one restart, after the last one.

A restart drops BlueZ's agent registration (#632), so the pairing agent is
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
import time
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from lib import bluez_dbus

from .bluez_agent import register_pairing_agent
from .bluez_records import RecordOutcome, write_device_record
from .config import get_bluez_restart_mode, get_restart_quiet_period

logger = logging.getLogger("psmove-pairing")

DEFAULT_GAME_STATE_URL = "http://localhost/joustmania.game_coordinator.GameCoordinatorService/GetGameState"
GAME_STATE_TIMEOUT_SECONDS = 2.0
# Players hold connected controllers from the countdown until the results
BUSY_GAME_STATES = frozenset({"STARTING", "RUNNING", "ENDING"})
# Time for bluetoothd to come back and re-read its storage
RESTART_SETTLE_SECONDS = 3.0
# Wait before the next attempt after 1, 2, 3, 4+ consecutive failed restarts (#1490)
RESTART_RETRY_BACKOFF_SECONDS = (10.0, 30.0, 60.0, 300.0)
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

    The restart drops BlueZ's agent registration (#632); without a new one a
    ZCM2 has no PIN agent. Once systemd has accepted the restart, the agent is
    registered again even if the wait is cut short. When ``RestartUnit`` itself
    fails, no job was queued and bluetoothd keeps running with the current agent
    registration, so the agent is left alone: a new one would only open a window
    without any agent.
    """
    await bluez_dbus.restart_systemd_unit("bluetooth.service")
    try:
        await asyncio.sleep(RESTART_SETTLE_SECONDS)
    finally:
        await register_pairing_agent()


@dataclass(frozen=True)
class PendingRecord:
    """A device record bluetoothd has not loaded yet (UC-001 A7, BR-008)."""

    adapter: str
    controller: str
    product_id: int
    bluetooth_dir: Path


class BluezReloader:
    """Applies pairing.bluez_restart_mode; holds the restart (and its records) between polls.

    Each pairing calls request(); the USB poll calls run_pending() once after
    all controllers of that poll, so N new controllers cost one restart.
    """

    def __init__(
        self,
        game_busy: Callable[[], Awaitable[bool]] = primary_game_busy,
        restart: Callable[[], Awaitable[None]] = restart_bluetoothd,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._game_busy = game_busy
        self._restart = restart
        self._clock = clock
        self.pending = False
        # Records waiting for the restart, by (adapter, controller)
        self.records: dict[tuple[str, str], PendingRecord] = {}
        # Consecutive failed restarts, and the clock time before which none is tried again
        self.failures = 0
        self._retry_at = 0.0
        # Clock time of the last USB poll that listed a PS Move; None = none seen yet (#2091)
        self.usb_seen_at: float | None = None

    def note_usb_controllers(self, count: int) -> None:
        """Called after each successful USB listing; a listed PS Move restarts the quiet period."""
        if count:
            self.usb_seen_at = self._clock()

    def _usb_busy(self) -> bool:
        """A PS Move was on USB within pairing.restart_quiet_period (UC-001 BR-005, #2091)."""
        quiet = get_restart_quiet_period()
        return quiet > 0 and self.usb_seen_at is not None and self._clock() - self.usb_seen_at < quiet

    def request(self, record: PendingRecord | None = None) -> str:
        """Called after a successful pairing; ``record`` = a record bluetoothd must load.

        Returns ``pending`` (a restart will be tried by run_pending) or ``skipped``.
        """
        mode = get_bluez_restart_mode()
        if mode == "never":
            self._clear()
            return "skipped"
        if record is not None:
            self.records[(record.adapter, record.controller)] = record
        if mode == "always" or record is not None:
            self.pending = True
        return "pending" if self.pending else "skipped"

    async def run_pending(self) -> str:
        """Run a pending restart if the mode allows it now; returns the outcome.

        Outcomes: ``skipped`` (nothing to do), ``restarted``, ``deferred``
        (when_idle and a real game is busy), ``waiting_usb`` (when_idle and a
        PS Move was on USB within the quiet period, #2091), ``failed``,
        ``backoff`` (an earlier attempt failed and its retry wait has not
        passed). After ``deferred``, ``waiting_usb``, ``failed`` and ``backoff``
        the restart and its records stay pending.
        Failed restarts are retried at most once per poll, after 10 s, 30 s,
        60 s, then every 5 min until one succeeds (#1490).
        """
        if not self.pending:
            return "skipped"
        mode = get_bluez_restart_mode()
        if mode == "never":
            logger.info("Dropping a pending bluetoothd restart: pairing.bluez_restart_mode is never")
            self._clear()
            return "skipped"
        if self.failures and self._clock() < self._retry_at:
            return "backoff"
        if mode == "when_idle" and self._usb_busy():
            return "waiting_usb"
        if mode == "when_idle" and await self._game_busy():
            logger.info("Deferring the bluetoothd restart until the real game has ended")
            return "deferred"
        await self._rewrite_missing_records()
        logger.info(f"Restarting bluetoothd (pairing.bluez_restart_mode={mode})")
        try:
            await self._restart()
        except Exception as e:
            # bluetoothd has not loaded the records: keep them for a later attempt
            wait = RESTART_RETRY_BACKOFF_SECONDS[min(self.failures, len(RESTART_RETRY_BACKOFF_SECONDS) - 1)]
            self.failures += 1
            self._retry_at = self._clock() + wait
            log = logger.error if self.failures == 1 else logger.warning
            log(f"Failed to restart bluetooth service ({self.failures} in a row): {e}; retrying in {wait:.0f} s")
            return "failed"
        self._clear()
        return "restarted"

    async def _rewrite_missing_records(self) -> None:
        """Write again any pending record that vanished while the restart waited.

        bluetoothd may delete a stored record it did not load (e.g. when a
        temporary device of that address expires) before the deferred restart.
        """
        for record in self.records.values():
            outcome = await asyncio.to_thread(
                write_device_record, record.adapter, record.controller, record.product_id, record.bluetooth_dir
            )
            if outcome is RecordOutcome.WRITTEN:
                logger.warning(f"BlueZ record of {record.controller} on {record.adapter} was gone; wrote it again")
            elif outcome is not RecordOutcome.EXISTS:
                logger.error(f"BlueZ record of {record.controller} on {record.adapter} is not usable: {outcome.value}")

    def _clear(self) -> None:
        self.pending = False
        self.records.clear()
        self.failures = 0
        self._retry_at = 0.0
