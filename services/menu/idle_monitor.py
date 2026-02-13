"""Idle monitor for convention battery saving mode.

Detects prolonged lobby inactivity and transitions controllers to idle state
(LEDs off) to conserve battery. Maintains 1-2 "sentinel" controllers with a
slow color pulse as a visual "system alive" indicator, rotating which
controllers serve as sentinels.

Any button press wakes ALL idle controllers back to CONNECTED state.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import TYPE_CHECKING

from services.menu import metrics
from services.menu.handlers.base import ControllerState

if TYPE_CHECKING:
    from services.menu.state_manager import StateManager

logger = logging.getLogger(__name__)

# Sentinel LED parameters
_SENTINEL_UPDATE_HZ = 10  # LED update rate for sentinel pulse
_SENTINEL_MIN_BRIGHTNESS = 0.09  # 9% minimum brightness
_SENTINEL_MAX_BRIGHTNESS = 0.30  # 30% maximum brightness
_SENTINEL_BREATH_PERIOD = 4.0  # Seconds per breathing cycle
_SENTINEL_HUE_PERIOD = 30.0  # Seconds for full hue rotation


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    """Convert HSV to RGB (0-255 scale)."""
    if s == 0.0:
        val = int(v * 255)
        return (val, val, val)

    i = int(h * 6.0)
    f = (h * 6.0) - i
    p = int(v * (1.0 - s) * 255)
    q = int(v * (1.0 - s * f) * 255)
    t = int(v * (1.0 - s * (1.0 - f)) * 255)
    val = int(v * 255)

    i %= 6
    if i == 0:
        return (val, t, p)
    if i == 1:
        return (q, val, p)
    if i == 2:
        return (p, val, t)
    if i == 3:
        return (p, q, val)
    if i == 4:
        return (t, p, val)
    return (val, p, q)


class IdleMonitor:
    """
    Monitors lobby idle time and manages idle/sentinel controller states.

    Responsibilities:
    - Track lobby idle time, reset on any button press
    - After timeout, transition CONNECTED controllers to IDLE (LEDs off)
    - Select sentinel controllers with slow color pulse
    - Rotate sentinels periodically
    - Wake all controllers on any button press from IDLE
    """

    def __init__(
        self,
        state_manager: StateManager,
        get_idle_enabled: callable,
        get_idle_timeout: callable,
        get_sentinel_count: callable,
        get_rotation_minutes: callable,
    ):
        """
        Initialize idle monitor.

        Args:
            state_manager: Menu state manager for controller transitions
            get_idle_enabled: Callable returning bool for idle mode enabled
            get_idle_timeout: Callable returning int for timeout in minutes
            get_sentinel_count: Callable returning int for number of sentinels
            get_rotation_minutes: Callable returning int for rotation interval
        """
        self._state_manager = state_manager
        self._get_idle_enabled = get_idle_enabled
        self._get_idle_timeout = get_idle_timeout
        self._get_sentinel_count = get_sentinel_count
        self._get_rotation_minutes = get_rotation_minutes

        self._last_activity_time: float = time.monotonic()
        self._idle_active: bool = False
        self._sentinel_serials: list[str] = []
        self._monitor_task: asyncio.Task | None = None
        self._sentinel_task: asyncio.Task | None = None
        self._last_rotation_time: float = 0.0

    @property
    def is_idle_active(self) -> bool:
        """Whether idle mode is currently engaged."""
        return self._idle_active

    @property
    def sentinel_serials(self) -> list[str]:
        """Currently active sentinel controller serials."""
        return list(self._sentinel_serials)

    def reset_activity(self) -> None:
        """Reset the idle timer on any controller activity."""
        self._last_activity_time = time.monotonic()

    def start(self) -> None:
        """Start the idle monitoring task."""
        if self._monitor_task is not None and not self._monitor_task.done():
            return

        self._last_activity_time = time.monotonic()
        self._idle_active = False
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("Idle monitor started")

    def stop(self) -> None:
        """Stop the idle monitoring task and sentinel effects."""
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            self._monitor_task = None

        self._stop_sentinel_task()
        self._idle_active = False
        self._sentinel_serials.clear()
        metrics.idle_mode_active.set(0)
        metrics.idle_controllers_count.set(0)
        metrics.sentinel_controllers_count.set(0)
        logger.info("Idle monitor stopped")

    async def wake_all(self, trigger_serial: str) -> None:
        """
        Wake all idle controllers back to CONNECTED state.

        Called when any controller wakes from idle via button press.

        Args:
            trigger_serial: Serial of the controller that triggered the wake
        """
        if not self._idle_active:
            return

        logger.info(f"Waking all idle controllers (triggered by {trigger_serial})")

        # Stop sentinel effects first
        self._stop_sentinel_task()

        # Transition all IDLE controllers back to CONNECTED
        idle_serials = [
            serial
            for serial, state in self._state_manager.controller_states.items()
            if state == ControllerState.IDLE and serial != trigger_serial
        ]

        for serial in idle_serials:
            await self._state_manager.transition_to(serial, ControllerState.CONNECTED)

        self._idle_active = False
        self._sentinel_serials.clear()
        self._last_activity_time = time.monotonic()

        # Update metrics
        metrics.idle_mode_active.set(0)
        metrics.idle_controllers_count.set(0)
        metrics.sentinel_controllers_count.set(0)
        metrics.idle_mode_wake_total.inc()

        logger.info(f"Woke {len(idle_serials) + 1} controllers from idle")

    async def _monitor_loop(self) -> None:
        """Background task monitoring lobby idle time."""
        try:
            while True:
                await asyncio.sleep(1.0)

                if not self._get_idle_enabled():
                    continue

                if self._idle_active:
                    # Check for sentinel rotation
                    await self._check_sentinel_rotation()
                    continue

                timeout_minutes = self._get_idle_timeout()
                timeout_seconds = timeout_minutes * 60
                elapsed = time.monotonic() - self._last_activity_time

                if elapsed >= timeout_seconds:
                    await self._enter_idle_mode()

        except asyncio.CancelledError:  # NOSONAR — intentional: background loop exits on cancellation
            logger.debug("Idle monitor loop cancelled")

    async def _enter_idle_mode(self) -> None:
        """Transition all CONNECTED controllers to IDLE and start sentinels."""
        connected_serials = [
            serial
            for serial, state in self._state_manager.controller_states.items()
            if state == ControllerState.CONNECTED
        ]

        if not connected_serials:
            return

        logger.info(f"Entering idle mode for {len(connected_serials)} controllers")

        # Select sentinels before transitioning (prefer highest battery)
        sentinel_count = self._get_sentinel_count()
        self._sentinel_serials = self._select_sentinels(connected_serials, sentinel_count)

        # Transition non-sentinel controllers to IDLE
        for serial in connected_serials:
            if serial not in self._sentinel_serials:
                await self._state_manager.transition_to(serial, ControllerState.IDLE)

        # Transition sentinel controllers to IDLE too (their LEDs will be managed by sentinel task)
        for serial in self._sentinel_serials:
            await self._state_manager.transition_to(serial, ControllerState.IDLE)

        self._idle_active = True
        self._last_rotation_time = time.monotonic()

        # Start sentinel LED effect
        if self._sentinel_serials:
            self._start_sentinel_task()

        # Update metrics
        idle_count = sum(1 for s in self._state_manager.controller_states.values() if s == ControllerState.IDLE)
        metrics.idle_mode_active.set(1)
        metrics.idle_controllers_count.set(idle_count)
        metrics.sentinel_controllers_count.set(len(self._sentinel_serials))
        metrics.idle_mode_entries_total.inc()

    def _select_sentinels(self, serials: list[str], count: int) -> list[str]:
        """
        Select sentinel controllers, preferring highest battery level.

        Args:
            serials: Available controller serials
            count: Number of sentinels to select

        Returns:
            List of selected sentinel serials
        """
        if count <= 0 or not serials:
            return []

        count = min(count, len(serials))

        # Sort by battery level (highest first), using serial as tiebreaker for stability
        sorted_serials = sorted(
            serials,
            key=lambda s: (self._state_manager.battery_levels.get(s, 0), s),
            reverse=True,
        )

        return sorted_serials[:count]

    async def _check_sentinel_rotation(self) -> None:
        """Check if sentinels should be rotated and perform rotation if needed."""
        rotation_minutes = self._get_rotation_minutes()
        if rotation_minutes <= 0:
            return

        elapsed = time.monotonic() - self._last_rotation_time
        if elapsed < rotation_minutes * 60:
            return

        # Get all idle controllers
        idle_serials = [
            serial for serial, state in self._state_manager.controller_states.items() if state == ControllerState.IDLE
        ]

        if not idle_serials:
            return

        sentinel_count = self._get_sentinel_count()

        # Select new sentinels, excluding current ones if possible
        non_sentinel_idle = [s for s in idle_serials if s not in self._sentinel_serials]
        candidates = non_sentinel_idle or idle_serials

        new_sentinels = self._select_sentinels(candidates, sentinel_count)

        if set(new_sentinels) != set(self._sentinel_serials):
            logger.info(f"Rotating sentinels: {self._sentinel_serials} -> {new_sentinels}")

            # Turn off old sentinels
            for serial in self._sentinel_serials:
                if serial not in new_sentinels:
                    await self._state_manager.led.set_color(serial, (0, 0, 0))

            self._sentinel_serials = new_sentinels
            metrics.sentinel_controllers_count.set(len(self._sentinel_serials))

        self._last_rotation_time = time.monotonic()

    def _start_sentinel_task(self) -> None:
        """Start the sentinel LED pulse task."""
        self._stop_sentinel_task()
        self._sentinel_task = asyncio.create_task(self._sentinel_pulse_loop())

    def _stop_sentinel_task(self) -> None:
        """Stop the sentinel LED pulse task."""
        if self._sentinel_task is not None:
            self._sentinel_task.cancel()
            self._sentinel_task = None

    async def _sentinel_pulse_loop(self) -> None:
        """Animate sentinel controllers with a slow breathing HSV color pulse."""
        interval = 1.0 / _SENTINEL_UPDATE_HZ
        start_time = time.monotonic()

        try:
            while True:
                elapsed = time.monotonic() - start_time

                # Breathing brightness: sine wave between min and max
                breath_phase = (elapsed / _SENTINEL_BREATH_PERIOD) * 2 * math.pi
                brightness_range = _SENTINEL_MAX_BRIGHTNESS - _SENTINEL_MIN_BRIGHTNESS
                brightness = _SENTINEL_MIN_BRIGHTNESS + (brightness_range * (0.5 + 0.5 * math.sin(breath_phase)))

                # Slow hue rotation
                hue = (elapsed / _SENTINEL_HUE_PERIOD) % 1.0

                # Convert to RGB
                color = _hsv_to_rgb(hue, 1.0, brightness)

                # Send to all current sentinels
                for serial in self._sentinel_serials:
                    await self._state_manager.led.set_color(serial, color)

                await asyncio.sleep(interval)

        except asyncio.CancelledError:  # NOSONAR — intentional: cleanup LEDs before exiting
            # Turn off sentinel LEDs on cancel
            for serial in self._sentinel_serials:
                await self._state_manager.led.set_color(serial, (0, 0, 0))
