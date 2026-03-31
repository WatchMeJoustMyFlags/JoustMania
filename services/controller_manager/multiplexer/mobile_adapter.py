"""
MobileAdapter — thin I/O adapter for phone-based controllers.

Phone sensor data arrives via gRPC (MobileControllerService) and is buffered
in PhoneConnection objects. The adapter's poll() reads from these buffers
with no network I/O (<1us).

All methods are sync (blocking) — called via asyncio.to_thread().
"""

from __future__ import annotations

import logging
import threading
import time

from lib.controller_constants import AxisKey, ButtonKey, StateKey
from services.controller_manager.multiplexer.adapter import ControllerIOAdapter

logger = logging.getLogger(__name__)

# Button bitmask positions (matches phone UI)
_BUTTON_MOVE = 1 << 0
_BUTTON_PS = 1 << 1


class PhoneConnection:
    """Thread-safe buffer for a single phone controller.

    Written by gRPC servicer (update_motion), read by adapter poll().
    Output state (LED/rumble) written by set_output_state(), read by
    gRPC servicer to send back to gateway.
    """

    def __init__(self, serial: str, name: str = ""):
        self.serial = serial
        self.name = name
        self.connected_at = time.time()

        self._lock = threading.Lock()
        # Motion state
        self._accel = {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 1.0}
        self._gyro = {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 0.0}
        self._buttons: int = 0
        self._battery: int = 100
        self._last_update: float = time.time()
        self._seq: int = 0

        # Output state (LED + rumble)
        self._led = {"r": 0, "g": 0, "b": 0}
        self._rumble: int = 0
        self._output_changed = threading.Event()

    def update_motion(
        self,
        accel_x: float,
        accel_y: float,
        accel_z: float,
        gyro_x: float,
        gyro_y: float,
        gyro_z: float,
        buttons: int,
        battery: int,
        seq: int,
    ) -> None:
        """Update motion data from phone sensor stream."""
        with self._lock:
            self._accel = {AxisKey.X: accel_x, AxisKey.Y: accel_y, AxisKey.Z: accel_z}
            self._gyro = {AxisKey.X: gyro_x, AxisKey.Y: gyro_y, AxisKey.Z: gyro_z}
            self._buttons = buttons
            self._battery = battery
            self._last_update = time.time()
            self._seq = seq

    def read_state(self) -> dict:
        """Read current state for poll(). Fast, no network."""
        with self._lock:
            return {
                StateKey.SERIAL: self.serial,
                StateKey.BATTERY: self._battery,
                StateKey.TRIGGER: 0,
                ButtonKey.MOVE: bool(self._buttons & _BUTTON_MOVE),
                ButtonKey.TRIGGER: False,
                ButtonKey.PS: bool(self._buttons & _BUTTON_PS),
                ButtonKey.SELECT: False,
                ButtonKey.START: False,
                ButtonKey.TRIANGLE: False,
                ButtonKey.CIRCLE: False,
                ButtonKey.CROSS: False,
                ButtonKey.SQUARE: False,
                StateKey.ACCEL: self._accel.copy(),
                StateKey.GYRO: self._gyro.copy(),
                StateKey.TEMPERATURE: 0,
                "connection_type": "mobile",
            }

    def set_output_state(self, r: int, g: int, b: int, rumble: int) -> None:
        """Set LED color and rumble. Signals output_changed event."""
        with self._lock:
            self._led = {"r": r, "g": g, "b": b}
            self._rumble = rumble
        self._output_changed.set()

    def get_pending_output(self) -> tuple[dict, int] | None:
        """Get pending output if changed. Returns (led_dict, rumble) or None."""
        if not self._output_changed.is_set():
            return None
        self._output_changed.clear()
        with self._lock:
            return self._led.copy(), self._rumble

    @property
    def last_update(self) -> float:
        with self._lock:
            return self._last_update


class MobileAdapter(ControllerIOAdapter):
    """I/O adapter for phone-based controllers.

    Phones are registered/unregistered by MobileControllerService gRPC.
    Sensor data flows through PhoneConnection buffers — poll() reads
    from these buffers with no network I/O.
    """

    def __init__(self):
        self._phones: dict[str, PhoneConnection] = {}
        self._lock = threading.Lock()
        self._next_index = 0
        logger.info("MobileAdapter initialized")

    @property
    def adapter_type(self) -> str:
        return "mobile"

    def discover(self, force: bool = False, verify_only: bool = False) -> list[str]:  # noqa: ARG002
        """Return serials of all connected phones."""
        with self._lock:
            return list(self._phones.keys())

    def open(self, serial: str) -> bool:  # NOSONAR(S3516)
        """No-op for mobile — phones are always accessible once registered."""
        with self._lock:
            return serial in self._phones

    def poll(self, serial: str) -> dict | None:
        """Read current sensor data from phone buffer. No network I/O."""
        with self._lock:
            phone = self._phones.get(serial)
        if phone is None:
            return None
        return phone.read_state()

    def set_output(self, serial: str, r: int, g: int, b: int, rumble: int) -> bool:
        """Store LED/rumble in phone buffer for gRPC stream to pick up."""
        with self._lock:
            phone = self._phones.get(serial)
        if phone is None:
            return False
        phone.set_output_state(r, g, b, rumble)
        return True

    def close(self, serial: str) -> None:
        """Remove a phone controller."""
        with self._lock:
            if serial in self._phones:
                del self._phones[serial]
                logger.info(f"Mobile: Closed phone {serial}")

    def close_all(self) -> None:
        """Remove all phone controllers."""
        with self._lock:
            self._phones.clear()

    # -- Methods for MobileControllerService --

    def register_phone(self, name: str = "") -> PhoneConnection:
        """Register a new phone controller. Returns PhoneConnection."""
        with self._lock:
            serial = f"PHONE{self._next_index:04d}"
            self._next_index += 1
            phone = PhoneConnection(serial, name=name)
            self._phones[serial] = phone
            logger.info(f"Mobile: Registered phone {serial} (name={name!r})")
            return phone

    def unregister_phone(self, serial: str) -> bool:
        """Unregister a phone controller."""
        with self._lock:
            if serial in self._phones:
                del self._phones[serial]
                logger.info(f"Mobile: Unregistered phone {serial}")
                return True
            return False

    def get_phone(self, serial: str) -> PhoneConnection | None:
        """Get PhoneConnection by serial."""
        with self._lock:
            return self._phones.get(serial)

    @property
    def phone_count(self) -> int:
        """Number of connected phones."""
        with self._lock:
            return len(self._phones)
