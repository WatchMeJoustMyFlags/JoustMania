"""
PsMoveAdapter — thin I/O adapter using the psmoveapi C library.

Extracts core I/O from BluetoothBackend. All methods are sync (blocking).
"""

from __future__ import annotations

import contextlib
import logging
import os
import time

from lib.controller_constants import AxisKey, ButtonKey, StateKey, normalize_serial
from services.controller_manager.multiplexer.adapter import ControllerIOAdapter


@contextlib.contextmanager
def suppress_stderr():
    """Suppress stderr output (e.g., psmoveapi calibration warnings)."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    old_stderr = os.dup(2)
    os.dup2(devnull, 2)
    try:
        yield
    finally:
        os.dup2(old_stderr, 2)
        os.close(devnull)
        os.close(old_stderr)


try:
    with suppress_stderr():
        import psmove

    PSMOVE_AVAILABLE = True
except ImportError:
    PSMOVE_AVAILABLE = False

logger = logging.getLogger(__name__)

# Accelerometer scale: raw ~4096 = 1g
ACCEL_SCALE = 4096.0


def _battery_to_percent(battery_value: int) -> int:
    """Convert psmove battery constant to percentage (0-100)."""
    if not PSMOVE_AVAILABLE:
        return 100
    if battery_value == psmove.Batt_CHARGING:
        return 100
    if battery_value in (psmove.Batt_CHARGING_DONE, psmove.Batt_MAX):
        return 100
    if battery_value == psmove.Batt_80Percent:
        return 80
    if battery_value == psmove.Batt_60Percent:
        return 60
    if battery_value == psmove.Batt_40Percent:
        return 40
    if battery_value == psmove.Batt_20Percent:
        return 20
    if battery_value == psmove.Batt_MIN:
        return 0
    logger.debug(f"Unknown battery value: {battery_value:#x}")
    return 50


class PsMoveAdapter(ControllerIOAdapter):
    """I/O adapter using psmoveapi C library for PS Move controllers.

    Handles are opened during discover() since psmove uses index-based API.
    """

    def __init__(self):
        if not PSMOVE_AVAILABLE:
            raise RuntimeError("psmove library not available")
        self._handles: dict[str, psmove.PSMove] = {}
        self._last_controller_count = 0

    @property
    def adapter_type(self) -> str:
        return "psmove"

    def discover(self, force: bool = False) -> list[str]:
        """Scan for connected PS Move controllers via psmove index API.

        Returns list of serials for all currently connected controllers.
        Handles are opened during discovery (psmove index-based API requires it).
        """
        count = psmove.count_connected()

        if count == self._last_controller_count and not force:
            return list(self._handles.keys())

        self._last_controller_count = count
        seen_serials = self._probe_controllers_with_retries(count)
        self._remove_stale_handles(seen_serials)
        return list(self._handles.keys())

    def _probe_controllers_with_retries(self, count: int) -> list[str]:
        """Probe controller indices with retries for flaky USB enumeration."""
        max_retries = 3
        retry_delay = 0.5
        seen_serials: list[str] = []

        for attempt in range(max_retries):
            seen_serials, failed_indices = self._probe_controller_indices(count)

            if len(seen_serials) >= count or not failed_indices:
                break

            if attempt < max_retries - 1:
                logger.info(f"Retry {attempt + 1}/{max_retries} in {retry_delay}s...")
                time.sleep(retry_delay)

        return seen_serials

    def _probe_controller_indices(self, count: int) -> tuple[list[str], list[int]]:
        """Probe each controller index, returning seen serials and failed indices."""
        seen_serials: list[str] = []
        failed_indices: list[int] = []

        for move_num in range(count):
            serial = self._probe_single_controller(move_num, count)
            if serial:
                seen_serials.append(serial)
            else:
                failed_indices.append(move_num)

        return seen_serials, failed_indices

    def _probe_single_controller(self, move_num: int, count: int) -> str | None:
        """Try to open a single controller index. Returns serial or None."""
        try:
            with suppress_stderr():
                move = psmove.PSMove(move_num)
            if move is None:
                logger.warning(f"Controller {move_num}/{count}: PSMove() returned None")
                return None

            serial = normalize_serial(move.get_serial())
            if not serial:
                logger.warning(f"Controller {move_num}/{count}: no serial returned")
                return None

            if serial not in self._handles:
                self._handles[serial] = move
                logger.info(f"New controller connected: {serial} (index {move_num})")
            return serial
        except Exception as e:
            logger.warning(f"Controller {move_num}/{count}: {e}")
            return None

    def _remove_stale_handles(self, seen_serials: list[str]) -> None:
        """Remove handles for controllers no longer in scan."""
        stale = set(self._handles.keys()) - set(seen_serials)
        for serial in stale:
            logger.info(f"Controller {serial} no longer in scan - removing stale handle")
            del self._handles[serial]

    def open(self, serial: str) -> bool:
        """Open is a no-op for psmove — handles are created during discover()."""
        return serial in self._handles

    def poll(self, serial: str) -> dict | None:
        """Read current sensor/button data from a PS Move controller."""
        move = self._handles.get(serial)
        if not move:
            return None

        try:
            while move.poll():
                pass  # Drain all queued reports to get latest state

            trigger = move.get_trigger()
            buttons = move.get_buttons()

            ax_raw, ay_raw, az_raw = move.get_accelerometer_frame(psmove.Frame_SecondHalf)
            gx, gy, gz = move.get_gyroscope_frame(psmove.Frame_SecondHalf)

            ax = ax_raw / ACCEL_SCALE
            ay = ay_raw / ACCEL_SCALE
            az = az_raw / ACCEL_SCALE

            battery_raw = move.get_battery()
            battery = _battery_to_percent(battery_raw)

            return {
                StateKey.SERIAL: serial,
                StateKey.BATTERY: battery,
                StateKey.TRIGGER: trigger,
                ButtonKey.MOVE: bool(buttons & psmove.Btn_MOVE),
                ButtonKey.TRIGGER: bool(buttons & psmove.Btn_T),
                ButtonKey.PS: bool(buttons & psmove.Btn_PS),
                ButtonKey.CROSS: bool(buttons & psmove.Btn_CROSS),
                ButtonKey.CIRCLE: bool(buttons & psmove.Btn_CIRCLE),
                ButtonKey.SQUARE: bool(buttons & psmove.Btn_SQUARE),
                ButtonKey.TRIANGLE: bool(buttons & psmove.Btn_TRIANGLE),
                ButtonKey.SELECT: bool(buttons & psmove.Btn_SELECT),
                ButtonKey.START: bool(buttons & psmove.Btn_START),
                StateKey.ACCEL: {AxisKey.X: ax, AxisKey.Y: ay, AxisKey.Z: az},
                StateKey.GYRO: {AxisKey.X: gx, AxisKey.Y: gy, AxisKey.Z: gz},
                StateKey.TEMPERATURE: move.get_temperature(),
            }

        except Exception as e:
            logger.error(f"Error reading controller state {serial}: {e}", exc_info=True)
            return None

    def set_output(self, serial: str, r: int, g: int, b: int, rumble: int) -> bool:
        """Write LED color and rumble to controller via psmoveapi."""
        move = self._handles.get(serial)
        if not move:
            return False

        try:
            move.set_leds(r, g, b)
            move.set_rumble(rumble)
            move.update_leds()
            return True
        except Exception as e:
            logger.error(f"Error setting output for {serial}: {e}", exc_info=True)
            return False

    def close(self, serial: str) -> None:
        """Release controller handle."""
        self._handles.pop(serial, None)

    def close_all(self) -> None:
        """Turn off all LEDs/rumble and release all handles."""
        for move in self._handles.values():
            with contextlib.suppress(Exception):
                move.set_leds(0, 0, 0)
                move.set_rumble(0)
                move.update_leds()
        self._handles.clear()
