"""
Linux/BlueZ Backend for PS Move Controllers

Uses psmove library + BlueZ/DBus for controller access on Raspberry Pi/Linux.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from typing import TYPE_CHECKING

from lib.controller_constants import (
    AxisKey,
    ButtonKey,
    StateKey,
    normalize_serial,
)
from services.controller_manager.backend import ControllerBackend

if TYPE_CHECKING:
    from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery


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


# Import Linux-specific dependencies (suppress psmove warnings)
try:
    with suppress_stderr():
        import psmove

    from services.controller_manager import bluetooth
    from services.controller_manager.controller_state import ControllerState

    LINUX_DEPS_AVAILABLE = True
except ImportError:
    LINUX_DEPS_AVAILABLE = False
    logging.warning("Linux dependencies (psmove, dbus) not available")

logger = logging.getLogger(__name__)


def _battery_to_percent(battery_value: int) -> int:
    """
    Convert psmove battery constant to percentage (0-100).

    psmove constants:
    - Batt_MIN = 0x00 -> 0%
    - Batt_20Percent = 0x01 -> 20%
    - Batt_40Percent = 0x02 -> 40%
    - Batt_60Percent = 0x03 -> 60%
    - Batt_80Percent = 0x04 -> 80%
    - Batt_MAX = 0x05 -> 100%
    - Batt_CHARGING = 0xEE -> 100% (charging)
    - Batt_CHARGING_DONE = 0xEF -> 100%
    """
    if not LINUX_DEPS_AVAILABLE:
        return 100

    if battery_value == psmove.Batt_CHARGING:
        return 100  # Treat charging as full for display purposes
    if battery_value == psmove.Batt_CHARGING_DONE or battery_value == psmove.Batt_MAX:
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
    # Unknown value, log and return mid-range
    logger.debug(f"Unknown battery value: {battery_value:#x}")
    return 50


class BluetoothBackend(ControllerBackend):
    """
    Linux BlueZ backend for PS Move controllers.

    Uses:
    - psmove library for controller I/O
    - BlueZ via DBus for Bluetooth operations

    Note: Controller pairing is handled by the host psmove-pairing daemon.
    This backend only handles Bluetooth-connected controllers.
    """

    def __init__(self, bt_discovery: CentralizedBTDiscovery | None = None):
        if not LINUX_DEPS_AVAILABLE:
            raise RuntimeError("Linux dependencies not available. Install: psmove, dbus-python, controller_state")

        self.controllers: dict[str, psmove.PSMove] = {}  # serial -> PSMove object
        self.controller_states: dict[str, ControllerState] = {}  # serial -> ControllerState
        self.led_colors: dict[str, tuple[int, int, int]] = {}  # serial -> (r, g, b) - track desired LED state
        self._bt_discovery = bt_discovery  # None = single-adapter mode
        self.hci = "hci0"  # Default, updated in initialize()

        self.running = False
        self._last_controller_count = 0  # Track count to avoid redundant rescans

        # Thread safety for LED operations
        self._led_lock = threading.Lock()
        # Controllers with active effects - polling skips LED refresh for these
        self._effect_active: set[str] = set()
        # Track last LED update time for keep-alive refresh
        self._last_led_update: dict[str, float] = {}
        # Phase 71: Track last color actually sent to each controller
        self._last_sent_color: dict[str, tuple[int, int, int]] = {}

        logger.info("BluetoothBackend initialized")

    async def initialize(self) -> bool:
        """Initialize Bluetooth adapter and scan for Bluetooth-connected controllers."""
        try:
            await self._initialize_bt_adapter()
            self._scan_existing_controllers()
            self.running = True
            return True

        except Exception as e:
            logger.error(f"Failed to initialize Bluetooth backend: {e}", exc_info=True)
            return False

    async def _initialize_bt_adapter(self) -> None:
        """Detect and enable Bluetooth adapter(s)."""
        if self._bt_discovery:
            adapters = await self._bt_discovery.initialize()
            self.hci = next(iter(adapters)) if adapters else "hci0"
            logger.info(f"Multi-adapter mode: {len(adapters)} adapter(s), primary={self.hci}")
        else:
            try:
                adapters = await bluetooth.get_hci_dict()
                if adapters:
                    self.hci = next(iter(adapters))
                    logger.info(f"Auto-detected Bluetooth adapter: {self.hci} ({adapters[self.hci]})")
            except Exception as e:
                logger.warning(f"Adapter detection failed, using {self.hci}: {e}")
            await bluetooth.enable_adapter(self.hci)
            logger.info(f"Enabled Bluetooth adapter: {self.hci}")

    def _scan_existing_controllers(self) -> None:
        """Scan for existing Bluetooth-connected PS Move controllers."""
        count = psmove.count_connected()
        logger.info(f"Found {count} PS Move controllers")

        for move_num in range(count):
            try:
                with suppress_stderr():
                    move = psmove.PSMove(move_num)
                if move is None:
                    continue

                serial = normalize_serial(move.get_serial())
                if not serial:
                    logger.debug(f"Controller {move_num}: no serial, skipping")
                    continue

                self.controllers[serial] = move
                self.controller_states[serial] = ControllerState()
                logger.info(f"Controller {serial}: ready")
            except Exception as e:
                logger.debug(f"Error initializing controller {move_num}: {e}")
                continue

    async def scan_controllers(self) -> list[dict]:
        """Scan for available controllers."""
        controllers = []

        try:
            # Get all attached devices via BlueZ (multi-adapter or single)
            if self._bt_discovery:
                devices = await self._bt_discovery.get_all_attached_addresses()
            else:
                devices = await bluetooth.get_attached_addresses(self.hci)

            for address in devices:
                # Check if it's a PS Move controller (MAC prefix 00:06:F7)
                if address.startswith("00:06:F7"):
                    controllers.append(
                        {"address": address, "serial": normalize_serial(address), "name": "PS Move Controller"}
                    )

            # Also check currently connected via psmove
            count = psmove.count_connected()
            for move_num in range(count):
                try:
                    with suppress_stderr():
                        move = psmove.PSMove(move_num)
                    if move is None:
                        continue
                    serial = normalize_serial(move.get_serial())
                    if not serial:
                        continue

                    # Add if not already in list
                    if not any(c["serial"] == serial for c in controllers):
                        controllers.append(
                            {
                                "address": serial,  # Use serial as address for connected controllers
                                "serial": serial,
                                "name": f"PS Move {serial[-4:]}",
                            }
                        )
                except Exception as e:
                    logger.debug(f"Error scanning controller {move_num}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Error scanning controllers: {e}", exc_info=True)

        return controllers

    async def connect_controller(self, address: str) -> bool:
        """
        Connect a controller by address.

        Note: Controller pairing is handled by the host psmove-pairing daemon.
        This method only tracks already-paired Bluetooth controllers.
        """
        try:
            # Check if already tracked
            if address in self.controllers:
                logger.info(f"Controller {address} already connected")
                return True

            # Scan for the controller
            for i in range(psmove.count_connected()):
                try:
                    with suppress_stderr():
                        move = psmove.PSMove(i)
                    if move is None:
                        continue
                    serial = normalize_serial(move.get_serial())
                    if not serial:
                        continue

                    if serial == normalize_serial(address):
                        # Track the controller
                        self.controllers[serial] = move
                        self.controller_states[serial] = ControllerState()
                        logger.info(f"Connected controller {serial}")
                        return True
                except Exception as e:
                    logger.debug(f"Error checking controller {i}: {e}")
                    continue

            logger.warning(f"Controller {address} not found")
            return False

        except Exception as e:
            logger.error(f"Error connecting controller {address}: {e}", exc_info=True)
            return False

    async def disconnect_controller(self, serial: str) -> bool:
        """Disconnect a controller."""
        try:
            # Remove from tracking
            if serial in self.controllers:
                del self.controllers[serial]
            if serial in self.controller_states:
                del self.controller_states[serial]
            # Phase 71: Clean up LED tracking
            self.led_colors.pop(serial, None)
            self._last_sent_color.pop(serial, None)
            self._last_led_update.pop(serial, None)
            self._effect_active.discard(serial)

            logger.info(f"Disconnected controller {serial}")
            return True

        except Exception as e:
            logger.error(f"Error disconnecting controller {serial}: {e}", exc_info=True)
            return False

    def _get_move_by_serial(self, serial: str) -> psmove.PSMove | None:
        """Get a fresh PSMove handle for a serial number."""
        count = psmove.count_connected()
        for i in range(count):
            try:
                with suppress_stderr():
                    move = psmove.PSMove(i)
                if move and normalize_serial(move.get_serial()) == serial:
                    return move
            except Exception:
                continue
        return None

    async def get_controller_state(self, serial: str) -> dict | None:
        """Get current controller state.

        Phase 72: LED updates removed from polling path for better performance.
        LED updates now happen via update_all_leds() called separately.
        """
        # Use stored handle for fast polling - don't create new handles each time
        move = self.controllers.get(serial)
        if not move:
            # Try to get handle if not stored
            move = self._get_move_by_serial(serial)
            if not move:
                # Controller disconnected - clean up all tracking
                if serial in self.controllers:
                    logger.warning(f"Controller {serial} no longer available")
                    del self.controllers[serial]
                    if serial in self.controller_states:
                        del self.controller_states[serial]
                    # Phase 71: Clean up LED tracking
                    self.led_colors.pop(serial, None)
                    self._last_sent_color.pop(serial, None)
                    self._last_led_update.pop(serial, None)
                    self._effect_active.discard(serial)
                return None
            self.controllers[serial] = move

        try:
            # Drain poll buffer so sensor reads return latest data
            while move.poll():
                pass  # NOSONAR - intentional: psmove API requires draining

            # Get controller state
            state = self.controller_states.get(serial)
            if not state:
                self.controller_states[serial] = ControllerState()

            # Read inputs
            trigger = move.get_trigger()
            buttons = move.get_buttons()

            # Read motion sensors (raw values)
            ax_raw, ay_raw, az_raw = move.get_accelerometer_frame(psmove.Frame_SecondHalf)
            gx, gy, gz = move.get_gyroscope_frame(psmove.Frame_SecondHalf)

            # Convert accelerometer to g-force units (raw ~4096 = 1g)
            # Standing still: z ≈ 1.0g (gravity), x/y ≈ 0
            accel_scale = 4096.0
            ax = ax_raw / accel_scale
            ay = ay_raw / accel_scale
            az = az_raw / accel_scale

            # Get battery (convert psmove constant to percentage)
            battery_raw = move.get_battery()
            battery = _battery_to_percent(battery_raw)

            # Build state dict with all button states
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

    def update_all_leds(self) -> int:
        """Update LEDs for all controllers that need it.

        Phase 72: Separated from get_controller_state() for better performance.
        Called from discovery loop at a fixed rate (e.g., 20Hz).

        Returns:
            Number of controllers updated
        """
        current_time = time.time()
        updated_count = 0

        for serial, stored_color in self.led_colors.items():
            # Skip if effect is active - effect controls LEDs directly
            if serial in self._effect_active:
                continue

            # Skip if controller not tracked
            move = self.controllers.get(serial)
            if not move:
                continue

            try:
                last_sent = self._last_sent_color.get(serial)
                last_led_update = self._last_led_update.get(serial, 0)

                # Update if: color changed (immediate) OR 4s elapsed (keep-alive)
                color_changed = stored_color != last_sent
                keepalive_needed = current_time - last_led_update >= 4.0

                if color_changed or keepalive_needed:
                    r, g, b = stored_color
                    with self._led_lock:
                        move.set_leds(r, g, b)
                        move.update_leds()
                    self._last_sent_color[serial] = stored_color
                    self._last_led_update[serial] = current_time
                    updated_count += 1

                    if color_changed:
                        logger.debug(f"LED color changed for {serial}: {last_sent} -> {stored_color}")

            except Exception as e:
                logger.debug(f"Error updating LED for {serial}: {e}")

        return updated_count

    async def set_led_color(self, serial: str, r: int, g: int, b: int) -> bool:
        """Set LED color on controller."""
        move = self.controllers.get(serial)
        if not move:
            return False

        try:
            # Track desired LED color so it can be reapplied during state polling
            self.led_colors[serial] = (r, g, b)
            with self._led_lock:
                move.set_leds(r, g, b)
                move.update_leds()
            # Phase 71: Track what was actually sent and when
            self._last_sent_color[serial] = (r, g, b)
            self._last_led_update[serial] = time.time()
            return True

        except Exception as e:
            logger.error(f"Error setting LED color {serial}: {e}", exc_info=True)
            return False

    def set_effect_active(self, serial: str, active: bool):
        """Mark controller as having active effect (polling skips LED refresh)."""
        if active:
            self._effect_active.add(serial)
        else:
            self._effect_active.discard(serial)

    async def set_rumble(self, serial: str, intensity: int) -> bool:
        """Set rumble intensity on controller."""
        move = self.controllers.get(serial)
        if not move:
            return False

        try:
            move.set_rumble(intensity)
            return True

        except Exception as e:
            logger.error(f"Error setting rumble {serial}: {e}", exc_info=True)
            return False

    def _probe_controller(self, move_num: int, count: int) -> str | None:
        """Probe a single controller index and register it if new.

        Args:
            move_num: The psmove index to probe.
            count: Total number of connected controllers (for log messages).

        Returns:
            The controller serial if successfully probed, or None on failure.
        """
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

            if serial not in self.controllers:
                # New controller detected - store the handle
                self.controllers[serial] = move
                self.controller_states[serial] = ControllerState()
                logger.info(f"New controller connected: {serial} (index {move_num})")
            # If serial already tracked, let the PSMove object be garbage collected
            # to avoid invalidating the existing handle
            return serial
        except Exception as e:
            logger.warning(f"Controller {move_num}/{count}: {e}")
            return None

    def _scan_controllers_with_retries(self, count: int, max_retries: int = 3, retry_delay: float = 0.5) -> list[str]:
        """Scan all controller indices with retry logic for unreliable hardware.

        New controllers may not be immediately ready after connecting. This method
        retries the full scan up to max_retries times with a delay between attempts.

        Args:
            count: Number of controllers reported by psmove.count_connected().
            max_retries: Maximum number of scan attempts.
            retry_delay: Seconds to wait between retries.

        Returns:
            List of serial numbers successfully probed.
        """
        for attempt in range(max_retries):
            seen_serials = []
            has_failures = False

            for move_num in range(count):
                serial = self._probe_controller(move_num, count)
                if serial:
                    seen_serials.append(serial)
                else:
                    has_failures = True

            # If we found all controllers or no failures, we're done
            if len(seen_serials) >= count or not has_failures:
                break

            # Retry after delay if we're missing controllers
            if attempt < max_retries - 1:
                logger.info(f"Retry {attempt + 1}/{max_retries} in {retry_delay}s...")
                time.sleep(retry_delay)

        return seen_serials

    def _remove_stale_controllers(self, seen_serials: list[str]) -> None:
        """Remove controllers that are no longer detected in a scan.

        Cleans up all tracking data (controller handle, state, LED colors, effects)
        for controllers that were previously tracked but not found in the latest scan.

        Args:
            seen_serials: List of serial numbers found in the most recent scan.
        """
        stale_serials = set(self.controllers.keys()) - set(seen_serials)
        for stale_serial in stale_serials:
            logger.info(f"Controller {stale_serial} no longer in scan - removing stale handle")
            del self.controllers[stale_serial]
            if stale_serial in self.controller_states:
                del self.controller_states[stale_serial]
            self.led_colors.pop(stale_serial, None)
            self._last_sent_color.pop(stale_serial, None)
            self._last_led_update.pop(stale_serial, None)
            self._effect_active.discard(stale_serial)

    def get_connected_controllers(self, force_rescan: bool = False) -> list[str]:
        """
        Get list of connected Bluetooth controller serials.

        Args:
            force_rescan: If True, bypass count-based optimization and do full scan.
                         Used by periodic discovery to catch externally paired controllers.

        Only rescans for new controllers when count_connected() changes to avoid
        creating duplicate PSMove handles that could invalidate existing ones.
        """
        try:
            count = psmove.count_connected()

            if count != self._last_controller_count or force_rescan:
                if count != self._last_controller_count:
                    logger.info(
                        f"Controller count changed: {self._last_controller_count} -> {count}, "
                        f"tracked: {len(self.controllers)}"
                    )
                self._last_controller_count = count

                seen_serials = self._scan_controllers_with_retries(count)
                self._remove_stale_controllers(seen_serials)

                logger.debug(
                    f"Scan complete: found {len(seen_serials)} serials: {seen_serials}, "
                    f"now tracking {len(self.controllers)}: {list(self.controllers.keys())}"
                )

        except Exception as e:
            logger.error(f"Error scanning controllers: {e}")

        return list(self.controllers.keys())

    async def shutdown(self):
        """Cleanup and shutdown."""
        logger.info("Shutting down Bluetooth backend")

        # Turn off all LEDs
        for _serial, move in self.controllers.items():
            try:
                move.set_leds(0, 0, 0)
                move.update_leds()
                move.set_rumble(0)
            except Exception:
                pass

        self.controllers.clear()
        self.controller_states.clear()
        self.running = False
        logger.info("Bluetooth backend shutdown complete")
