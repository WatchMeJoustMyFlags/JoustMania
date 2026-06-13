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


def _create_controller_state(serial: str, reserved: bool = False, tag: str = "") -> dict:
    """Create a new controller state dict with default values.

    Args:
        serial: Controller serial number.
        reserved: When True, the controller is hidden from button-stream
            consumers (the menu). It stays fully usable via gameplay-data
            streams and explicit-serial LED/feedback commands.
        tag: Identifies the owning agent/game (orphan cleanup, debugging).
    """
    return {
        StateKey.SERIAL: serial,
        "reserved": reserved,
        "tag": tag,
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
        # Number of gameplay-stream frames the death-level acceleration must
        # still be DELIVERED in. Decremented by the gameplay send path
        # (consume_death_frame), NOT by a wall-clock timer — see #926. A
        # wall-clock latch could be skipped entirely by a send loop starved
        # under parallel-game CPU load, so the death never reached the game
        # and the session never converged. Counting delivered frames makes the
        # death cadence-independent: it is guaranteed to cross the EMA death
        # threshold regardless of how slowly frames are streamed.
        "death_frames_remaining": 0,
        # Wall-clock safety cap: clears an unconsumed death latch for a
        # controller that is NOT currently in a game (no gameplay stream is
        # draining it), so a stray SimulateDeath never sticks forever.
        "death_hold_until": 0.0,
    }


class MockAdapter(ControllerIOAdapter):
    """Simulated I/O adapter for testing without hardware.

    Provides the ControllerIOAdapter interface plus extra methods
    needed by MockControllerService (add_controller, remove_controller,
    add_observer, remove_observer, get_led_color, controllers property).
    """

    def __init__(self, num_controllers: int = 0):
        self._num_controllers = num_controllers
        self.controllers: dict[str, dict] = {}
        self._observers: list[asyncio.Queue] = []
        logger.info(f"MockAdapter initialized with {num_controllers} controllers")

    @property
    def adapter_type(self) -> str:
        return "mock"

    def discover(
        self,
        force: bool = False,  # noqa: ARG002
        verify_only: bool = False,  # noqa: ARG002
        exclude_serials: list[str] | None = None,  # noqa: ARG002
    ) -> list[str]:
        """Return all mock controller serials.

        On first call (empty controllers dict), creates the initial set
        if num_controllers > 0.  When num_controllers is 0 (the default),
        controllers are added dynamically via add_controller() / RPC.
        """
        if not self.controllers and self._num_controllers > 0:
            for i in range(self._num_controllers):
                serial = f"mock_controller_{i}"
                self.controllers[serial] = _create_controller_state(serial)
                logger.info(f"Created mock controller: {serial}")
        return list(self.controllers.keys())

    def open(self, serial: str) -> bool:  # NOSONAR(S3516) always True — mock controllers never fail to open
        """Open is a no-op for mock — controllers are always accessible."""
        if serial not in self.controllers:
            self.controllers[serial] = _create_controller_state(serial)
        return True

    def poll(self, serial: str) -> dict | None:
        """Generate mock sensor data with noise and death simulation."""
        controller = self.controllers.get(serial)
        if not controller:
            return None

        current_time = time.time()

        # Death-level acceleration is held until it has been DELIVERED to the
        # game enough times to cross the EMA death threshold (#926). The
        # frame budget is decremented by the gameplay send path
        # (consume_death_frame), not here — poll() only reports the current
        # latch state. A wall-clock safety cap clears a latch that no gameplay
        # stream is draining (controller not in a game).
        death_accel = None
        death_active = controller["death_accel"] and (
            controller["death_frames_remaining"] > 0
            and (controller["death_hold_until"] == 0.0 or controller["death_hold_until"] > current_time)
        )
        if death_active:
            death_accel = controller["death_accel"]
        else:
            if controller["death_accel"] is not None:
                # Latch expired (budget drained or safety cap passed): revert.
                controller["death_accel"] = None
                controller["death_frames_remaining"] = 0
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

    def consume_death_frame(self, serial: str) -> None:
        """Account one DELIVERED gameplay frame against a pending death latch.

        Called by the gameplay-stream send path (``_build_gameplay_update``)
        once per controller per streamed frame. While a death latch is active
        this decrements its delivered-frame budget; when the budget reaches
        zero the latch clears on the next ``poll()`` and acceleration reverts
        to noise.

        This is the mechanism that makes mock deaths converge independently of
        host load (#926): the death-level acceleration is guaranteed to appear
        in exactly ``death_frames_remaining`` streamed frames — enough to cross
        the EMA death threshold — no matter how slowly the send loop runs. A
        previous wall-clock hold could be skipped wholesale by a starved send
        loop, so the death never reached the game and the session stalled with
        all players alive.

        No-op for controllers without an active latch.
        """
        controller = self.controllers.get(serial)
        if not controller or not controller["death_accel"]:
            return
        if controller["death_frames_remaining"] > 0:
            controller["death_frames_remaining"] -= 1

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

    def add_controller(self, serial: str | None = None, reserved: bool = False, tag: str = "") -> str:
        """Add a new mock controller dynamically.

        Args:
            serial: Controller serial; auto-generated if None.
            reserved: When True, hide the controller from button-stream
                consumers (the menu). See _create_controller_state.
            tag: Identifies the owning agent/game (orphan cleanup, debugging).
        """
        if serial is None:
            serial = f"MOCK{len(self.controllers):04d}"
        if serial in self.controllers:
            logger.warning(f"Mock: Controller {serial} already exists")
            return serial
        self.controllers[serial] = _create_controller_state(serial, reserved=reserved, tag=tag)
        logger.info(f"Mock: Added controller {serial} (reserved={reserved}, tag={tag!r})")
        return serial

    def get_metadata(self, serial: str) -> dict | None:
        """Return reserved/tag metadata for a controller, or None if unknown."""
        controller = self.controllers.get(serial)
        if not controller:
            return None
        return {
            "reserved": controller.get("reserved", False),
            "tag": controller.get("tag", ""),
        }

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
