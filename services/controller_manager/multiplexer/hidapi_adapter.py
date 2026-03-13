"""
HidapiAdapter — thin I/O adapter using the hidapi library for HID communication.

Extracts core I/O from HidapiBackend. All methods are sync (blocking).
"""

from __future__ import annotations

import contextlib
import logging

import hidraw as hid  # hidraw backend sees Bluetooth HID devices (libusb backend cannot)

from lib.controller_constants import AxisKey, ButtonKey, StateKey, normalize_serial
from lib.psmove_hid import (
    ALL_PRODUCT_IDS,
    INPUT_REPORT_SIZE,
    PRODUCT_ID_ZCM2,
    PRODUCT_ID_ZCM2E,
    VENDOR_ID,
    Button,
    battery_to_percent,
    build_output_report,
    parse_input_report,
)
from services.controller_manager.multiplexer.adapter import ControllerIOAdapter

logger = logging.getLogger(__name__)

# Accelerometer scale factor: raw ~4096 per 1g
ACCEL_SCALE = 4096.0


class HidapiAdapter(ControllerIOAdapter):
    """I/O adapter using hidapi for PS Move controllers via HID reports."""

    def __init__(self):
        self._devices: dict[str, hid.device] = {}
        self._paths: dict[str, str] = {}
        self._product_ids: dict[str, int] = {}
        self._last_state: dict[str, dict] = {}  # cached last-known state per serial

    @property
    def adapter_type(self) -> str:
        return "hidapi"

    def discover(self, force: bool = False, verify_only: bool = False) -> list[str]:
        """Enumerate HID devices and open new PS Move controllers."""
        if not force and self._devices:
            self._verify_existing_devices()

        if not verify_only:
            current_paths = self._enumerate_and_open_new()

            if force:
                self._remove_disappeared_devices(current_paths)

        return list(self._devices.keys())

    def _verify_existing_devices(self) -> None:
        """Check existing devices are still accessible, clean up stale ones."""
        stale = []
        for serial, device in self._devices.items():
            try:
                device.read(64)  # Non-blocking health check
            except BlockingIOError:
                pass  # No data available is fine — device is healthy
            except OSError:
                stale.append(serial)
        for serial in stale:
            self._cleanup(serial)

    def _enumerate_and_open_new(self) -> set[str]:
        """Enumerate HID devices, open new ones. Returns set of current device paths."""
        devices = [d for pid in ALL_PRODUCT_IDS for d in hid.enumerate(VENDOR_ID, pid)]
        current_paths: set[str] = set()

        for dev_info in devices:
            path = dev_info["path"]
            serial = dev_info.get("serial_number", "")
            if not serial:
                continue

            serial = normalize_serial(serial)
            current_paths.add(path)

            if serial not in self._devices:
                self._try_open_device(serial, path, dev_info.get("product_id", 0))

        return current_paths

    def _try_open_device(self, serial: str, path: str, product_id: int = 0) -> None:
        """Attempt to open a single HID device."""
        try:
            device = hid.device()
            device.open_path(path)
            device.set_nonblocking(True)
            self._devices[serial] = device
            self._paths[serial] = path
            self._product_ids[serial] = product_id
            logger.info(f"Opened PS Move controller: {serial} at {path!r}")
        except Exception as e:
            logger.warning(f"Failed to open PS Move at {path!r}: {e}")

    def _remove_disappeared_devices(self, current_paths: set[str]) -> None:
        """Remove controllers whose device paths are no longer present."""
        stale_serials = [s for s, p in self._paths.items() if p not in current_paths]
        for serial in stale_serials:
            logger.info(f"Controller {serial} no longer present - removing")
            self._cleanup(serial)

    def open(self, serial: str) -> bool:
        """Open a specific controller by serial (re-enumerate if needed)."""
        if serial in self._devices:
            return True

        devices = [d for pid in ALL_PRODUCT_IDS for d in hid.enumerate(VENDOR_ID, pid)]
        for dev_info in devices:
            dev_serial = dev_info.get("serial_number", "")
            if not dev_serial:
                continue
            dev_serial = normalize_serial(dev_serial)
            if dev_serial == normalize_serial(serial):
                try:
                    device = hid.device()
                    device.open_path(dev_info["path"])
                    device.set_nonblocking(True)
                    self._devices[serial] = device
                    self._paths[serial] = dev_info["path"]
                    self._product_ids[serial] = dev_info.get("product_id", 0)
                    return True
                except Exception as e:
                    logger.error(f"Failed to open controller {serial}: {e}")
                    return False

        return False

    def poll(self, serial: str) -> dict | None:
        """Read current controller state from HID input report."""
        device = self._devices.get(serial)
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
                # No new HID report available (normal for non-blocking reads).
                # Return cached state so callers don't count this as a drop.
                return self._last_state.get(serial)

            is_zcm2 = self._product_ids.get(serial) in (PRODUCT_ID_ZCM2, PRODUCT_ID_ZCM2E)
            parsed = parse_input_report(data, zcm2=is_zcm2)

            ax_raw, ay_raw, az_raw = parsed["accel"][1]
            gx_raw, gy_raw, gz_raw = parsed["gyro"][1]

            ax = ax_raw / ACCEL_SCALE
            ay = ay_raw / ACCEL_SCALE
            az = az_raw / ACCEL_SCALE

            gx = float(gx_raw)
            gy = float(gy_raw)
            gz = float(gz_raw)

            battery = battery_to_percent(parsed["battery"])
            buttons = parsed["buttons"]

            state = {
                StateKey.SERIAL: serial,
                StateKey.BATTERY: battery,
                StateKey.TRIGGER: parsed["trigger"][1],
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
            self._last_state[serial] = state
            return state

        except OSError as e:
            logger.warning(f"I/O error reading controller {serial}: {e}")
            self._cleanup(serial)
            return None
        except Exception as e:
            logger.error(f"Error reading controller state {serial}: {e}", exc_info=True)
            return None

    def set_output(self, serial: str, r: int, g: int, b: int, rumble: int) -> bool:
        """Write LED color and rumble via HID output report."""
        device = self._devices.get(serial)
        if not device:
            return False

        try:
            report = build_output_report(r=r, g=g, b=b, rumble=rumble)
            device.write(report)
            return True
        except OSError as e:
            logger.warning(f"I/O error setting output for {serial}: {e}")
            self._cleanup(serial)
            return False
        except Exception as e:
            logger.error(f"Error setting output for {serial}: {e}", exc_info=True)
            return False

    def close(self, serial: str) -> None:
        """Release controller handle."""
        self._cleanup(serial)

    def close_all(self) -> None:
        """Turn off all LEDs/rumble and close all devices."""
        for device in self._devices.values():
            with contextlib.suppress(Exception):
                report = build_output_report(r=0, g=0, b=0, rumble=0)
                device.write(report)
                device.close()
        self._devices.clear()
        self._paths.clear()
        self._product_ids.clear()
        self._last_state.clear()

    def _cleanup(self, serial: str) -> None:
        """Remove a controller that is no longer accessible."""
        device = self._devices.pop(serial, None)
        if device:
            with contextlib.suppress(Exception):
                device.close()
        self._paths.pop(serial, None)
        self._product_ids.pop(serial, None)
        self._last_state.pop(serial, None)
        logger.info(f"Cleaned up controller {serial}")
