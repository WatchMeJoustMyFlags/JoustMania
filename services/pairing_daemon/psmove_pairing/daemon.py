"""Main PS Move pairing daemon."""

import asyncio
import logging
import shutil
import time

from opentelemetry import trace

from .bluetooth_monitor import BluetoothMonitor
from .bluez_agent import ensure_pairing_agent, register_pairing_agent, set_known_controller_check
from .config import METRICS_PORT, get_bt_monitor_interval, get_poll_interval, is_debug
from .usb_pairing import USBPairing
from .utils import run_command

logger = logging.getLogger("psmove-pairing")

# Health check thresholds
_HEALTH_STALENESS_THRESHOLD = 60.0  # seconds
_PREREQUISITE_TIMEOUT = 5.0  # seconds per startup command (#1770)


class PairingDaemon:
    """PS Move controller pairing daemon with async Bluetooth monitoring."""

    def __init__(self, tracer: trace.Tracer):
        self.tracer = tracer
        self.usb_pairing = USBPairing(tracer)
        # The pairing agent answers only for controllers paired over USB this session (UC-001 BR-009)
        set_known_controller_check(self.usb_pairing.paired_this_session)
        self.bt_monitor = BluetoothMonitor(tracer)

        # Health tracking timestamps
        self._last_usb_poll: float = 0.0
        self._last_bt_monitor: float = 0.0
        self._startup_time: float = time.time()

        # Event loop the daemon runs on; the HTTP thread schedules unpairs onto it (UC-005)
        self.loop: asyncio.AbstractEventLoop | None = None

        logger.info("PairingDaemon initialized")

    async def validate_prerequisites(self) -> bool:
        """Validate that required tools are available.

        Returns True if all prerequisites are met, False otherwise.
        """
        errors = []

        # Check bluetoothctl
        if not shutil.which("bluetoothctl"):
            errors.append("bluetoothctl not found")
        else:
            exit_code, output = await run_command(["bluetoothctl", "show"], timeout_seconds=_PREREQUISITE_TIMEOUT)
            if exit_code != 0:
                errors.append(f"bluetoothctl failed: {output}")

        # Check hciconfig (for Bluetooth monitoring)
        if not shutil.which("hciconfig"):
            errors.append("hciconfig not found")

        # Check hcitool (for RSSI monitoring)
        if not shutil.which("hcitool"):
            errors.append("hcitool not found")

        if errors:
            for error in errors:
                logger.error(f"Prerequisite check failed: {error}")
            return False

        logger.info("All prerequisites validated successfully")
        return True

    def is_healthy(self) -> bool:
        """Check if daemon is healthy (loops running within threshold).

        Returns True if both USB poll and Bluetooth monitor have run recently.
        """
        now = time.time()

        # During startup, allow grace period
        if now - self._startup_time < _HEALTH_STALENESS_THRESHOLD:
            return True

        # Check USB poll staleness
        # A loop that never completed is measured from daemon start, not from the epoch (#1770)
        usb_age = now - (self._last_usb_poll or self._startup_time)
        bt_age = now - (self._last_bt_monitor or self._startup_time)
        usb_stale = usb_age > _HEALTH_STALENESS_THRESHOLD
        bt_stale = bt_age > _HEALTH_STALENESS_THRESHOLD

        if usb_stale:
            since = "last poll" if self._last_usb_poll else "start (never polled)"
            logger.warning(f"USB poll loop stale: {usb_age:.1f}s since {since}")
        if bt_stale:
            since = "last monitor" if self._last_bt_monitor else "start (never monitored)"
            logger.warning(f"BT monitor loop stale: {bt_age:.1f}s since {since}")

        return not (usb_stale or bt_stale)

    def get_health_status(self) -> dict:
        """Get detailed health status for /healthz endpoint."""
        now = time.time()
        return {
            "healthy": self.is_healthy(),
            "uptime_seconds": now - self._startup_time,
            "last_usb_poll_seconds_ago": now - self._last_usb_poll if self._last_usb_poll else None,
            "last_bt_monitor_seconds_ago": now - self._last_bt_monitor if self._last_bt_monitor else None,
            "usb_poll_count": self.usb_pairing.poll_count,
            "bt_monitor_count": self.bt_monitor.monitor_count,
        }

    def update_usb_poll_timestamp(self) -> None:
        """Update the last USB poll timestamp."""
        self._last_usb_poll = time.time()

    def update_bt_monitor_timestamp(self) -> None:
        """Update the last Bluetooth monitor timestamp."""
        self._last_bt_monitor = time.time()

    async def _usb_poll_loop(self) -> None:
        """USB polling loop with health tracking.

        Re-evaluates poll interval from flagd each iteration for runtime tunability.
        """
        logger.info(f"Starting USB poll loop (interval: {get_poll_interval()}s)")
        while True:
            try:
                await self.usb_pairing.poll()
                self.update_usb_poll_timestamp()
            except Exception as e:
                logger.error(f"Error during USB poll: {e}", exc_info=is_debug())
            await asyncio.sleep(get_poll_interval())

    async def _bt_monitor_loop(self) -> None:
        """Bluetooth monitoring loop with health tracking.

        Re-evaluates monitor interval from flagd each iteration for runtime tunability.
        """
        logger.info(f"Starting Bluetooth monitor loop (interval: {get_bt_monitor_interval()}s)")
        while True:
            try:
                await self.bt_monitor.monitor()
                self.update_bt_monitor_timestamp()
            except Exception as e:
                logger.error(f"Error during Bluetooth monitor: {e}", exc_info=is_debug())
            # Registers the pairing agent again while BlueZ has not confirmed it on the
            # current bus connection: bluetoothd not up yet, or a new connection (UC-001 BR-009)
            await ensure_pairing_agent()
            await asyncio.sleep(get_bt_monitor_interval())

    async def run(self) -> None:
        """Main daemon loop with concurrent USB polling and Bluetooth monitoring."""
        self.loop = asyncio.get_running_loop()
        logger.info("PS Move Pairing Daemon started")
        logger.info(f"  USB poll interval: {get_poll_interval()}s")
        logger.info(f"  Bluetooth monitor interval: {get_bt_monitor_interval()}s")
        logger.info(f"  debug mode: {is_debug()}")
        logger.info(f"  metrics port: {METRICS_PORT}")

        # Validate prerequisites
        if not await self.validate_prerequisites():
            logger.warning("Some prerequisites failed - daemon may not work correctly")

        # Register the BlueZ pairing agent (UC-001 BR-009)
        await register_pairing_agent()

        # Run both loops concurrently
        try:
            await asyncio.gather(
                self._usb_poll_loop(),
                self._bt_monitor_loop(),
            )
        finally:
            # Shutdown: end the LED cues on the controllers on USB (UC-001 BR-010, #2091)
            await self.usb_pairing.led_cue.close()
