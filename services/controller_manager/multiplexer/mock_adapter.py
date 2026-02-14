"""
MockAdapter — thin I/O adapter for simulated controllers.

Extracts core I/O from MockBackend. All methods are sync (blocking).
Provides extra methods for MockControllerService (add/remove controllers,
observer pattern, LED query).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time

from lib.controller_constants import AxisKey, ButtonKey, StateKey
from services.controller_manager.multiplexer.adapter import ControllerIOAdapter

logger = logging.getLogger(__name__)


def _create_controller_state(serial: str) -> dict:
    """Create a new controller state dict with default values."""
    return {
        StateKey.SERIAL: serial,
        StateKey.BATTERY: 100,
        StateKey.TRIGGER: 0,
        ButtonKey.MOVE: False,
        ButtonKey.TRIGGER: False,
        ButtonKey.PS: False,
        ButtonKey.SELECT: False,
        ButtonKey.START: False,
        ButtonKey.TRIANGLE: False,
        ButtonKey.CIRCLE: False,
        ButtonKey.CROSS: False,
        ButtonKey.SQUARE: False,
        StateKey.ACCEL: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 1.0},
        StateKey.GYRO: {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 0.0},
        StateKey.TEMPERATURE: 25,
        "led": {"r": 255, "g": 255, "b": 255},
        "rumble": 0,
        "connected_at": time.time(),
        "last_update": time.time(),
        "death_accel": None,
        "death_hold_until": 0.0,
    }


class MockAdapter(ControllerIOAdapter):
    """Simulated I/O adapter for testing without hardware.

    Provides the ControllerIOAdapter interface plus extra methods
    needed by MockControllerService (add_controller, remove_controller,
    add_observer, remove_observer, get_led_color, controllers property).
    """

    def __init__(self, num_controllers: int = 4):
        self._num_controllers = num_controllers
        self.controllers: dict[str, dict] = {}
        self._observers: list[asyncio.Queue] = []
        logger.info(f"MockAdapter initialized with {num_controllers} controllers")

    @property
    def adapter_type(self) -> str:
        return "mock"

    def discover(self, force: bool = False) -> list[str]:  # noqa: ARG002
        """Return all mock controller serials.

        On first call (empty controllers dict), creates the initial set.
        """
        if not self.controllers:
            for i in range(self._num_controllers):
                serial = f"mock_controller_{i}"
                self.controllers[serial] = _create_controller_state(serial)
                logger.info(f"Created mock controller: {serial}")
        return list(self.controllers.keys())

    def open(self, serial: str) -> bool:
        """Open is a no-op for mock — controllers are always accessible."""
        if serial in self.controllers:
            return True
        # Auto-create if not exists
        self.controllers[serial] = _create_controller_state(serial)
        return True

    def poll(self, serial: str) -> dict | None:
        """Generate mock sensor data with noise and death simulation."""
        controller = self.controllers.get(serial)
        if not controller:
            return None

        current_time = time.time()

        # Check if we're holding death acceleration
        death_accel = None
        if controller["death_hold_until"] > current_time and controller["death_accel"]:
            death_accel = controller["death_accel"]
        else:
            if controller["death_hold_until"] > 0 and controller["death_hold_until"] <= current_time:
                controller["death_accel"] = None
                controller["death_hold_until"] = 0.0

            # Add noise to accelerometer
            controller[StateKey.ACCEL][AxisKey.X] = random.gauss(0.0, 0.1)
            controller[StateKey.ACCEL][AxisKey.Y] = random.gauss(0.0, 0.1)
            controller[StateKey.ACCEL][AxisKey.Z] = random.gauss(1.0, 0.1)

        # Add noise to gyroscope
        controller[StateKey.GYRO][AxisKey.X] = random.gauss(0.0, 0.5)
        controller[StateKey.GYRO][AxisKey.Y] = random.gauss(0.0, 0.5)
        controller[StateKey.GYRO][AxisKey.Z] = random.gauss(0.0, 0.5)

        # Simulate battery drain
        time_since_connected = current_time - controller["connected_at"]
        hours_used = time_since_connected / 3600
        controller[StateKey.BATTERY] = max(0, 100 - int(hours_used * 20))

        controller["last_update"] = current_time

        return {
            StateKey.SERIAL: serial,
            StateKey.BATTERY: controller[StateKey.BATTERY],
            StateKey.TRIGGER: controller[StateKey.TRIGGER],
            ButtonKey.MOVE: controller[ButtonKey.MOVE],
            ButtonKey.TRIGGER: controller[ButtonKey.TRIGGER],
            ButtonKey.PS: controller[ButtonKey.PS],
            ButtonKey.SELECT: controller[ButtonKey.SELECT],
            ButtonKey.START: controller[ButtonKey.START],
            ButtonKey.TRIANGLE: controller[ButtonKey.TRIANGLE],
            ButtonKey.CIRCLE: controller[ButtonKey.CIRCLE],
            ButtonKey.CROSS: controller[ButtonKey.CROSS],
            ButtonKey.SQUARE: controller[ButtonKey.SQUARE],
            StateKey.ACCEL: death_accel.copy() if death_accel else controller[StateKey.ACCEL].copy(),
            StateKey.GYRO: controller[StateKey.GYRO].copy(),
            StateKey.TEMPERATURE: controller[StateKey.TEMPERATURE],
            "connection_type": "mock",
        }

    def set_output(self, serial: str, r: int, g: int, b: int, rumble: int) -> bool:
        """Store LED/rumble state and publish to observers."""
        controller = self.controllers.get(serial)
        if not controller:
            return False

        old_led = controller.get("led", {})
        old_rumble = controller.get("rumble", 0)

        controller["led"] = {"r": r, "g": g, "b": b}
        controller["rumble"] = rumble

        # Publish LED change event
        if (old_led.get("r"), old_led.get("g"), old_led.get("b")) != (r, g, b):
            self._publish_event(
                {
                    "type": "led_change",
                    "serial": serial,
                    "r": r,
                    "g": g,
                    "b": b,
                    "source": "set_output",
                    "timestamp_ms": int(time.time() * 1000),
                }
            )

        # Publish rumble change event
        if old_rumble != rumble:
            self._publish_event(
                {
                    "type": "rumble_change",
                    "serial": serial,
                    "intensity": rumble,
                    "timestamp_ms": int(time.time() * 1000),
                }
            )

        return True

    def close(self, serial: str) -> None:
        """Remove a controller."""
        if serial in self.controllers:
            del self.controllers[serial]
            logger.info(f"Mock: Closed controller {serial}")

    def close_all(self) -> None:
        """Remove all controllers."""
        self.controllers.clear()

    # -- Extra methods for MockControllerService --

    def add_controller(self, serial: str | None = None) -> str:
        """Add a new mock controller dynamically."""
        if serial is None:
            serial = f"MOCK{len(self.controllers):04d}"
        if serial in self.controllers:
            logger.warning(f"Mock: Controller {serial} already exists")
            return serial
        self.controllers[serial] = _create_controller_state(serial)
        logger.info(f"Mock: Added controller {serial}")
        return serial

    def remove_controller(self, serial: str) -> bool:
        """Remove a mock controller."""
        if serial in self.controllers:
            del self.controllers[serial]
            logger.info(f"Mock: Removed controller {serial}")
            return True
        return False

    def add_observer(self) -> asyncio.Queue:
        """Add an observer queue for state changes."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._observers.append(queue)
        logger.debug(f"Added observer, total: {len(self._observers)}")
        return queue

    def remove_observer(self, queue: asyncio.Queue) -> None:
        """Remove an observer queue."""
        if queue in self._observers:
            self._observers.remove(queue)
            logger.debug(f"Removed observer, remaining: {len(self._observers)}")

    def get_led_color(self, serial: str) -> tuple[int, int, int] | None:
        """Get current LED color for a controller."""
        controller = self.controllers.get(serial)
        if not controller:
            return None
        led = controller.get("led", {"r": 0, "g": 0, "b": 0})
        return (led["r"], led["g"], led["b"])

    def _publish_event(self, event: dict) -> None:
        """Publish event to all observers."""
        for queue in self._observers:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)
