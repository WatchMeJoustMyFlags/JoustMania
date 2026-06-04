"""
DeviceManager — dedicated HID I/O thread for PS Move controllers.

Runs blocking HID operations on a dedicated thread with a command channel
(queue.Queue). Matches the rust-hid architecture for apples-to-apples
canary comparison.

Commands: Discover, Open, Close, CloseAll, SetOutput, RegisterStream, UnregisterStream
"""

from __future__ import annotations

import contextlib
import enum
import logging
import pathlib
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import hidraw as hid

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

logger = logging.getLogger(__name__)

# Accelerometer scale factor: raw ~4096 per 1g
ACCEL_SCALE = 4096.0

# Polling interval for the HID read loop (~1kHz)
POLL_INTERVAL_S = 0.001


class CommandType(enum.Enum):
    DISCOVER = "discover"
    OPEN = "open"
    CLOSE = "close"
    CLOSE_ALL = "close_all"
    SET_OUTPUT = "set_output"
    REGISTER_STREAM = "register_stream"
    UNREGISTER_STREAM = "unregister_stream"
    SHUTDOWN = "shutdown"


@dataclass
class Command:
    type: CommandType
    args: dict[str, Any] = field(default_factory=dict)
    reply: queue.Queue | None = None


@dataclass
class SensorData:
    """Parsed sensor data from a PS Move controller."""

    serial: str
    battery: int
    trigger: int
    temperature: int
    button_move: bool
    button_trigger: bool
    button_ps: bool
    button_cross: bool
    button_circle: bool
    button_square: bool
    button_triangle: bool
    button_select: bool
    button_start: bool
    accel_x: float
    accel_y: float
    accel_z: float
    gyro_x: float
    gyro_y: float
    gyro_z: float


class DeviceManager:
    """Manages HID devices on a dedicated thread with command-based interface."""

    def __init__(self):
        self._command_queue: queue.Queue[Command] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False

        # Device state (only accessed from the device thread)
        self._devices: dict[str, hid.device] = {}
        self._paths: dict[str, str] = {}
        self._product_ids: dict[str, int] = {}
        self._adapter_names: dict[str, str] = {}

        # Stream subscribers (stream_id -> queue)
        self._stream_queues: dict[str, queue.Queue[SensorData]] = {}
        self._stream_lock = threading.Lock()

    def start(self) -> None:
        """Start the device manager thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name="python-hid-device-manager",
            daemon=True,
        )
        self._thread.start()
        logger.info("DeviceManager thread started")

    def stop(self) -> None:
        """Stop the device manager thread."""
        if not self._running:
            return
        self._running = False
        self._command_queue.put(Command(type=CommandType.SHUTDOWN))
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("DeviceManager thread stopped")

    def send_command(self, cmd: Command, timeout: float = 5.0) -> Any:
        """Send a command and wait for the reply."""
        cmd.reply = queue.Queue(maxsize=1)
        self._command_queue.put(cmd)
        try:
            return cmd.reply.get(timeout=timeout)
        except queue.Empty:
            logger.warning(f"Command {cmd.type} timed out after {timeout}s")
            return None

    def register_stream(self, stream_id: str) -> queue.Queue[SensorData]:
        """Register a stream subscriber. Returns the queue to read from."""
        q: queue.Queue[SensorData] = queue.Queue(maxsize=1024)
        with self._stream_lock:
            self._stream_queues[stream_id] = q
        return q

    def unregister_stream(self, stream_id: str) -> None:
        """Unregister a stream subscriber."""
        with self._stream_lock:
            self._stream_queues.pop(stream_id, None)

    def _run(self) -> None:
        """Main device thread loop: process commands and poll devices."""
        logger.info("DeviceManager thread running")
        try:
            while self._running:
                # Process pending commands (non-blocking)
                self._drain_commands()
                # Poll all open devices
                self._poll_all_devices()
                # Sleep to target ~1kHz
                time.sleep(POLL_INTERVAL_S)
        except Exception:
            logger.exception("DeviceManager thread crashed")
        finally:
            self._close_all_devices()
            logger.info("DeviceManager thread exiting")

    def _drain_commands(self) -> None:
        """Process all pending commands without blocking."""
        while True:
            try:
                cmd = self._command_queue.get_nowait()
            except queue.Empty:
                break

            if cmd.type == CommandType.SHUTDOWN:
                self._running = False
                if cmd.reply:
                    cmd.reply.put(True)
                break

            result = self._handle_command(cmd)
            if cmd.reply:
                cmd.reply.put(result)

    def _handle_command(self, cmd: Command) -> Any:
        """Dispatch a single command."""
        match cmd.type:
            case CommandType.DISCOVER:
                return self._discover(
                    force=cmd.args.get("force", False),
                    verify_only=cmd.args.get("verify_only", False),
                    exclude_serials=cmd.args.get("exclude_serials"),
                )
            case CommandType.OPEN:
                return self._open(cmd.args["serial"])
            case CommandType.CLOSE:
                self._close(cmd.args["serial"])
                return True
            case CommandType.CLOSE_ALL:
                self._close_all_devices()
                return True
            case CommandType.SET_OUTPUT:
                return self._set_output(
                    serial=cmd.args["serial"],
                    r=cmd.args["r"],
                    g=cmd.args["g"],
                    b=cmd.args["b"],
                    rumble=cmd.args["rumble"],
                )
            case _:
                logger.warning(f"Unknown command type: {cmd.type}")
                return None

    # -- Device operations (run on device thread only) --

    @staticmethod
    def _resolve_adapter_from_sysfs(hidraw_path: str | bytes) -> str | None:
        """Resolve BT adapter from hidraw device path via sysfs."""
        if isinstance(hidraw_path, bytes):
            hidraw_path = hidraw_path.decode("utf-8", errors="replace")
        hidraw_name = pathlib.Path(hidraw_path).name
        sysfs = pathlib.Path(f"/sys/class/hidraw/{hidraw_name}")
        if not sysfs.exists():
            return None
        resolved = str(sysfs.resolve())
        for part in resolved.split("/"):
            if part.startswith("hci") and ":" not in part:
                return part
        return None

    def _discover(
        self,
        force: bool = False,
        verify_only: bool = False,
        exclude_serials: list[str] | None = None,
    ) -> list[dict]:
        """Enumerate HID devices and open new PS Move controllers.

        Returns list of dicts with serial, adapter_name, product_id.
        """
        if not force and self._devices:
            self._verify_existing_devices()

        if not verify_only:
            current_paths = self._enumerate_and_open_new(exclude_serials)
            if force:
                self._remove_disappeared_devices(current_paths)

        # Return info about all known devices
        result = []
        for serial in self._devices:
            result.append(
                {
                    "serial": serial,
                    "adapter_name": self._adapter_names.get(serial, ""),
                    "product_id": self._product_ids.get(serial, 0),
                }
            )
        return result

    def _verify_existing_devices(self) -> None:
        """Check existing devices are still accessible."""
        stale = []
        for serial, device in self._devices.items():
            try:
                device.read(64)
            except BlockingIOError:
                pass
            except OSError:
                stale.append(serial)
        for serial in stale:
            self._cleanup(serial)

    def _enumerate_and_open_new(self, exclude_serials: list[str] | None = None) -> set[str]:
        """Enumerate HID devices, open new ones."""
        from lib.controller_constants import normalize_serial

        exclude = set(exclude_serials or [])
        devices = [d for pid in ALL_PRODUCT_IDS for d in hid.enumerate(VENDOR_ID, pid)]
        current_paths: set[str] = set()

        for dev_info in devices:
            path = dev_info["path"]
            serial = dev_info.get("serial_number", "")
            if not serial:
                continue

            serial = normalize_serial(serial)
            current_paths.add(path)

            if serial in exclude:
                continue

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
            adapter = self._resolve_adapter_from_sysfs(path)
            if adapter:
                self._adapter_names[serial] = adapter
            logger.info(f"Opened PS Move controller: {serial} at {path!r}")
        except Exception as e:
            logger.warning(f"Failed to open PS Move at {path!r}: {e}")

    def _remove_disappeared_devices(self, current_paths: set[str]) -> None:
        """Remove controllers whose device paths are no longer present."""
        stale_serials = [s for s, p in self._paths.items() if p not in current_paths]
        for serial in stale_serials:
            logger.info(f"Controller {serial} no longer present - removing")
            self._cleanup(serial)

    def _open(self, serial: str) -> bool:
        """Open a specific controller by serial."""
        from lib.controller_constants import normalize_serial

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

    def _set_output(self, serial: str, r: int, g: int, b: int, rumble: int) -> bool:
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

    def _close(self, serial: str) -> None:
        """Release controller handle."""
        self._cleanup(serial)

    def _close_all_devices(self) -> None:
        """Turn off all LEDs/rumble and close all devices."""
        for device in self._devices.values():
            with contextlib.suppress(Exception):
                report = build_output_report(r=0, g=0, b=0, rumble=0)
                device.write(report)
                device.close()
        self._devices.clear()
        self._paths.clear()
        self._product_ids.clear()
        self._adapter_names.clear()

    def _cleanup(self, serial: str) -> None:
        """Remove a controller that is no longer accessible."""
        device = self._devices.pop(serial, None)
        if device:
            with contextlib.suppress(Exception):
                device.close()
        self._paths.pop(serial, None)
        self._product_ids.pop(serial, None)
        self._adapter_names.pop(serial, None)
        logger.info(f"Cleaned up controller {serial}")

    def _poll_all_devices(self) -> None:
        """Read all open devices and push SensorData to registered streams."""
        if not self._devices:
            return

        with self._stream_lock:
            if not self._stream_queues:
                return
            active_queues = list(self._stream_queues.values())

        for serial, device in list(self._devices.items()):
            sensor_data = self._read_device(serial, device)
            if sensor_data is None:
                continue
            for q in active_queues:
                try:
                    q.put_nowait(sensor_data)
                except queue.Full:
                    # Drop oldest and add new
                    try:
                        q.get_nowait()
                        q.put_nowait(sensor_data)
                    except queue.Empty:
                        pass

    def _read_device(self, serial: str, device: hid.device) -> SensorData | None:
        """Read latest HID report from a single device."""
        try:
            data = None
            while True:
                try:
                    report = device.read(INPUT_REPORT_SIZE)
                except BlockingIOError:
                    break
                if not report:
                    break
                if isinstance(report, list):
                    report = bytes(report)
                data = report

            if data is None:
                return None

            # Diagnostic hex dump: log raw bytes when any button is pressed
            if any(b != 0 for b in data[1:5]):
                logger.debug(f"HID raw [{len(data)}B]: {data[:8].hex(' ')}")

            is_zcm2 = self._product_ids.get(serial) in (PRODUCT_ID_ZCM2, PRODUCT_ID_ZCM2E)
            parsed = parse_input_report(data, zcm2=is_zcm2)

            ax_raw, ay_raw, az_raw = parsed["accel"][1]
            gx_raw, gy_raw, gz_raw = parsed["gyro"][1]

            buttons = parsed["buttons"]

            return SensorData(
                serial=serial,
                battery=battery_to_percent(parsed["battery"]),
                trigger=parsed["trigger"][1],
                temperature=parsed["temperature"],
                button_move=bool(buttons & Button.MOVE),
                button_trigger=bool(buttons & Button.T),
                button_ps=bool(buttons & Button.PS),
                button_cross=bool(buttons & Button.CROSS),
                button_circle=bool(buttons & Button.CIRCLE),
                button_square=bool(buttons & Button.SQUARE),
                button_triangle=bool(buttons & Button.TRIANGLE),
                button_select=bool(buttons & Button.SELECT),
                button_start=bool(buttons & Button.START),
                accel_x=ax_raw / ACCEL_SCALE,
                accel_y=ay_raw / ACCEL_SCALE,
                accel_z=az_raw / ACCEL_SCALE,
                gyro_x=float(gx_raw),
                gyro_y=float(gy_raw),
                gyro_z=float(gz_raw),
            )

        except OSError as e:
            logger.warning(f"I/O error reading controller {serial}: {e}")
            self._cleanup(serial)
            return None
        except Exception as e:
            logger.error(f"Error reading controller state {serial}: {e}", exc_info=True)
            return None
