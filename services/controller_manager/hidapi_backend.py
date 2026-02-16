"""
Hidapi Backend for PS Move Controllers

Pure Python backend using the hidapi library for direct HID communication
with PS Move controllers over Bluetooth. Replaces the psmoveapi C library
dependency for controller I/O.

Requires:
- hidapi Python package (pip install hidapi)
- libhidapi-hidraw0 system library (apt install libhidapi-hidraw0)

Activated via controller_backend=hidapi flag in flagd (performance domain).
"""

import asyncio
import contextlib
import logging
import os
import threading
import time

import hidraw as hid  # hidraw backend sees Bluetooth HID devices (libusb backend cannot)

from lib.controller_constants import (
    AxisKey,
    ButtonKey,
    StateKey,
    normalize_serial,
)
from lib.psmove_hid import (
    ALL_PRODUCT_IDS,
    INPUT_REPORT_SIZE,
    VENDOR_ID,
    Button,
    battery_to_percent,
    build_output_report,
    parse_input_report,
)
from services.controller_manager.backend import ControllerBackend

logger = logging.getLogger(__name__)

# Accelerometer scale factor: raw value ~4096 per 1g
# PS Move accelerometer outputs unsigned 16-bit values offset by 0x8000.
# After subtracting 0x8000, the result is in raw units where ~4096 = 1g.
ACCEL_SCALE = 4096.0


class HidapiBackend(ControllerBackend):
    """
    Pure Python backend for PS Move controllers using hidapi.

    Uses the hidapi library for direct HID communication instead of
    psmoveapi. This eliminates the need for the C library build (cmake,
    swig, etc.) while providing the same functionality.

    Key differences from BluetoothBackend:
    - Uses device paths (e.g., /dev/hidraw3) instead of integer indices
    - More robust hot-plug handling (paths are stable per session)
    - No SWIG wrapper overhead
    """

    def __init__(self):
        # serial -> hid.device
        self.controllers: dict[str, hid.device] = {}
        # serial -> device path (for reconnection detection)
        self._device_paths: dict[str, str] = {}
        # LED state tracking
        self.led_colors: dict[str, tuple[int, int, int]] = {}
        self._last_sent_color: dict[str, tuple[int, int, int]] = {}
        self._last_led_update: dict[str, float] = {}
        # Thread safety for LED operations
        self._led_lock = threading.Lock()
        # Controllers with active effects
        self._effect_active: set[str] = set()

        self.running = False

        # Optional RSSI reader (requires NET_ADMIN capability)
        self._rssi_reader = None
        # Cached RSSI values (updated by periodic background task)
        self._rssi_cache: dict[str, int] = {}
        self._rssi_task: asyncio.Task | None = None
        if os.environ.get("HIDAPI_ENABLE_RSSI", "").lower() in ("1", "true"):
            try:
                from lib.hci_rssi import HciRssiReader

                self._rssi_reader = HciRssiReader()
                logger.info("HCI RSSI reader enabled")
            except Exception as e:
                logger.info(f"HCI RSSI reader not available: {e}")

        logger.info("HidapiBackend initialized")

    async def initialize(self) -> bool:
        """Initialize by scanning for connected PS Move controllers via hidapi."""
        try:
            self._scan_and_open()
            self.running = True
            logger.info(f"HidapiBackend initialized with {len(self.controllers)} controllers")

            # Start RSSI background task (~1Hz, separate from 100Hz polling)
            if self._rssi_reader:
                self._rssi_task = asyncio.create_task(self._rssi_poll_loop())
                logger.info("RSSI polling task started (1Hz)")

            return True
        except Exception as e:
            logger.error(f"Failed to initialize hidapi backend: {e}", exc_info=True)
            return False

    async def _rssi_poll_loop(self) -> None:
        """Background task that polls RSSI for all connected controllers at ~1Hz.

        RSSI readings use HCI sockets which are independent of hidapi data I/O.
        Running at 1Hz keeps overhead minimal while providing useful signal
        strength data for dashboards and connection quality monitoring.
        """
        while self.running:
            try:
                for serial in self.controllers:
                    rssi = self.get_rssi(serial)
                    if rssi is not None:
                        self._rssi_cache[serial] = rssi

                # Clean up stale entries
                stale = set(self._rssi_cache.keys()) - set(self.controllers.keys())
                for serial in stale:
                    del self._rssi_cache[serial]

            except Exception as e:
                logger.debug(f"RSSI poll error: {e}")

            await asyncio.sleep(1.0)

    def _scan_and_open(self) -> None:
        """Enumerate HID devices and open PS Move controllers."""
        devices = [d for pid in ALL_PRODUCT_IDS for d in hid.enumerate(VENDOR_ID, pid)]

        for dev_info in devices:
            path = dev_info["path"]
            serial = dev_info.get("serial_number", "")

            if not serial:
                logger.debug(f"HID device at {path!r}: no serial, skipping")
                continue

            serial = normalize_serial(serial)

            if serial in self.controllers:
                continue

            try:
                device = hid.device()
                device.open_path(path)
                device.set_nonblocking(True)
                self.controllers[serial] = device
                self._device_paths[serial] = path
                logger.info(f"Opened PS Move controller: {serial} at {path!r}")
            except Exception as e:
                logger.warning(f"Failed to open PS Move at {path!r}: {e}")

    async def scan_controllers(self) -> list[dict]:
        """Scan for available PS Move controllers."""
        controllers = []
        devices = [d for pid in ALL_PRODUCT_IDS for d in hid.enumerate(VENDOR_ID, pid)]

        for dev_info in devices:
            serial = dev_info.get("serial_number", "")
            if not serial:
                continue
            serial = normalize_serial(serial)
            if not any(c["serial"] == serial for c in controllers):
                controllers.append(
                    {
                        "address": serial,
                        "serial": serial,
                        "name": f"PS Move {serial[-4:]}" if len(serial) >= 4 else serial,
                    }
                )
        return controllers

    async def connect_controller(self, address: str) -> bool:
        """Connect a controller by serial number."""
        if address in self.controllers:
            logger.info(f"Controller {address} already connected")
            return True

        # Search for the device
        devices = [d for pid in ALL_PRODUCT_IDS for d in hid.enumerate(VENDOR_ID, pid)]

        for dev_info in devices:
            serial = dev_info.get("serial_number", "")
            if not serial:
                continue
            serial = normalize_serial(serial)
            if serial == normalize_serial(address):
                try:
                    device = hid.device()
                    device.open_path(dev_info["path"])
                    device.set_nonblocking(True)
                    self.controllers[serial] = device
                    self._device_paths[serial] = dev_info["path"]
                    logger.info(f"Connected controller {serial}")
                    return True
                except Exception as e:
                    logger.error(f"Failed to connect controller {address}: {e}")
                    return False

        logger.warning(f"Controller {address} not found")
        return False

    async def disconnect_controller(self, serial: str) -> bool:
        """Disconnect and close a controller."""
        device = self.controllers.pop(serial, None)
        if device:
            with contextlib.suppress(Exception):
                device.close()
        self._device_paths.pop(serial, None)
        self.led_colors.pop(serial, None)
        self._last_sent_color.pop(serial, None)
        self._last_led_update.pop(serial, None)
        self._effect_active.discard(serial)
        logger.info(f"Disconnected controller {serial}")
        return True

    async def get_controller_state(self, serial: str) -> dict | None:
        """Read current controller state from HID input report."""
        device = self.controllers.get(serial)
        if not device:
            return None

        try:
            # Read all available reports, keep the latest
            data = None
            while True:
                try:
                    report = device.read(INPUT_REPORT_SIZE)
                except BlockingIOError:
                    break  # No more data available (hidraw non-blocking)
                if not report:
                    break
                # hidraw returns list of ints; convert to bytes for struct parsing
                if isinstance(report, list):
                    report = bytes(report)
                data = report

            if data is None:
                # No new data available (non-blocking read returned empty)
                return None

            parsed = parse_input_report(data)

            # Use second frame for sensor data (matches psmoveapi's Frame_SecondHalf)
            ax_raw, ay_raw, az_raw = parsed["accel"][1]
            gx_raw, gy_raw, gz_raw = parsed["gyro"][1]

            # Convert accelerometer to g-force units
            ax = ax_raw / ACCEL_SCALE
            ay = ay_raw / ACCEL_SCALE
            az = az_raw / ACCEL_SCALE

            # Gyroscope: pass through raw values (matches psmoveapi behavior)
            gx = float(gx_raw)
            gy = float(gy_raw)
            gz = float(gz_raw)

            # Battery as percentage
            battery = battery_to_percent(parsed["battery"])

            # Decode buttons from bitmask
            buttons = parsed["buttons"]

            state = {
                StateKey.SERIAL: serial,
                StateKey.BATTERY: battery,
                StateKey.TRIGGER: parsed["trigger"][1],  # Second frame trigger
                ButtonKey.MOVE: bool(buttons & Button.MOVE),
                ButtonKey.TRIGGER: bool(buttons & Button.T),
                ButtonKey.PS: bool(buttons & Button.PS),
                ButtonKey.CROSS: bool(buttons & Button.CROSS),
                ButtonKey.CIRCLE: bool(buttons & Button.CIRCLE),
                ButtonKey.SQUARE: bool(buttons & Button.SQUARE),
                ButtonKey.TRIANGLE: bool(buttons & Button.TRIANGLE),
                ButtonKey.SELECT: bool(buttons & Button.SELECT),
                ButtonKey.START: bool(buttons & Button.START),
                StateKey.ACCEL: {AxisKey.X: ax, AxisKey.Y: ay, AxisKey.Z: az},
                StateKey.GYRO: {AxisKey.X: gx, AxisKey.Y: gy, AxisKey.Z: gz},
                StateKey.TEMPERATURE: parsed["temperature"],
            }

            # Include cached RSSI if available (updated by background task at ~1Hz)
            if serial in self._rssi_cache:
                state["rssi"] = self._rssi_cache[serial]

            return state

        except OSError as e:
            # Device disconnected or I/O error
            logger.warning(f"I/O error reading controller {serial}: {e}")
            self._cleanup_controller(serial)
            return None
        except Exception as e:
            logger.error(f"Error reading controller state {serial}: {e}", exc_info=True)
            return None

    def _cleanup_controller(self, serial: str) -> None:
        """Remove a controller that is no longer accessible."""
        device = self.controllers.pop(serial, None)
        if device:
            with contextlib.suppress(Exception):
                device.close()
        self._device_paths.pop(serial, None)
        self.led_colors.pop(serial, None)
        self._last_sent_color.pop(serial, None)
        self._last_led_update.pop(serial, None)
        self._effect_active.discard(serial)
        logger.info(f"Cleaned up disconnected controller {serial}")

    async def set_led_color(self, serial: str, r: int, g: int, b: int) -> bool:
        """Set LED color on controller."""
        device = self.controllers.get(serial)
        if not device:
            return False

        try:
            self.led_colors[serial] = (r, g, b)
            report = build_output_report(r=r, g=g, b=b)
            with self._led_lock:
                device.write(report)
            self._last_sent_color[serial] = (r, g, b)
            self._last_led_update[serial] = time.time()
            return True
        except OSError as e:
            logger.warning(f"I/O error setting LED for {serial}: {e}")
            self._cleanup_controller(serial)
            return False
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
        device = self.controllers.get(serial)
        if not device:
            return False

        try:
            # Get current LED color to preserve it
            current_color = self.led_colors.get(serial, (0, 0, 0))
            r, g, b = current_color
            report = build_output_report(r=r, g=g, b=b, rumble=intensity)
            with self._led_lock:
                device.write(report)
            return True
        except OSError as e:
            logger.warning(f"I/O error setting rumble for {serial}: {e}")
            self._cleanup_controller(serial)
            return False
        except Exception as e:
            logger.error(f"Error setting rumble {serial}: {e}", exc_info=True)
            return False

    def update_all_leds(self) -> int:
        """Update LEDs for all controllers that need it.

        Handles LED keep-alive (PS Move requires periodic refresh to maintain
        LED color) and detects color changes for immediate updates.

        Returns:
            Number of controllers updated.
        """
        current_time = time.time()
        updated_count = 0

        for serial, stored_color in self.led_colors.items():
            if serial in self._effect_active:
                continue

            device = self.controllers.get(serial)
            if not device:
                continue

            try:
                last_sent = self._last_sent_color.get(serial)
                last_led_update = self._last_led_update.get(serial, 0)

                color_changed = stored_color != last_sent
                keepalive_needed = current_time - last_led_update >= 4.0

                if color_changed or keepalive_needed:
                    r, g, b = stored_color
                    report = build_output_report(r=r, g=g, b=b)
                    with self._led_lock:
                        device.write(report)
                    self._last_sent_color[serial] = stored_color
                    self._last_led_update[serial] = current_time
                    updated_count += 1

                    if color_changed:
                        logger.debug(f"LED color changed for {serial}: {last_sent} -> {stored_color}")

            except OSError:
                logger.debug(f"I/O error updating LED for {serial} (likely disconnected)")
            except Exception as e:
                logger.debug(f"Error updating LED for {serial}: {e}")

        return updated_count

    def get_connected_controllers(self, force_rescan: bool = False) -> list[str]:
        """Get list of connected controller serials.

        Args:
            force_rescan: If True, re-enumerate HID devices to detect
                         newly connected controllers.
        """
        if force_rescan:
            # Re-enumerate to find new controllers
            devices = [d for pid in ALL_PRODUCT_IDS for d in hid.enumerate(VENDOR_ID, pid)]
            current_paths = set()

            for dev_info in devices:
                path = dev_info["path"]
                serial = dev_info.get("serial_number", "")
                if not serial:
                    continue
                serial = normalize_serial(serial)
                current_paths.add(path)

                if serial not in self.controllers:
                    try:
                        device = hid.device()
                        device.open_path(path)
                        device.set_nonblocking(True)
                        self.controllers[serial] = device
                        self._device_paths[serial] = path
                        logger.info(f"New controller found during rescan: {serial}")
                    except Exception as e:
                        logger.debug(f"Failed to open device at {path!r}: {e}")

            # Remove controllers whose device paths are gone
            stale_serials = [serial for serial, dev_path in self._device_paths.items() if dev_path not in current_paths]
            for serial in stale_serials:
                logger.info(f"Controller {serial} no longer present - removing")
                self._cleanup_controller(serial)

        return list(self.controllers.keys())

    def get_rssi(self, serial: str) -> int | None:
        """Read RSSI for a connected controller.

        Requires HIDAPI_ENABLE_RSSI=true and NET_ADMIN capability.
        The serial is converted to a Bluetooth MAC address format.

        Args:
            serial: Controller serial (e.g., "00064FEE1234").

        Returns:
            RSSI in dBm, or None if unavailable.
        """
        if not self._rssi_reader:
            return None

        # Convert serial to MAC address format (XX:XX:XX:XX:XX:XX)
        if len(serial) == 12 and ":" not in serial:
            address = ":".join(serial[i : i + 2] for i in range(0, 12, 2))
        else:
            address = serial

        return self._rssi_reader.get_rssi(address)

    async def shutdown(self):
        """Cleanup and shutdown all controller connections."""
        logger.info("Shutting down hidapi backend")

        # Cancel RSSI background task
        if self._rssi_task and not self._rssi_task.done():
            self._rssi_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._rssi_task

        for _serial, device in self.controllers.items():
            with contextlib.suppress(Exception):
                report = build_output_report(r=0, g=0, b=0, rumble=0)
                device.write(report)
                device.close()

        self.controllers.clear()
        self._device_paths.clear()
        self.led_colors.clear()
        self._last_sent_color.clear()
        self._last_led_update.clear()
        self._effect_active.clear()
        self.running = False

        if self._rssi_reader:
            self._rssi_reader.close()
            self._rssi_reader = None

        logger.info("Hidapi backend shutdown complete")
