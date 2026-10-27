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
import math
import random
import threading
import time
from dataclasses import dataclass, field

from lib.controller_constants import AxisKey, ButtonKey, StateKey
from services.controller_manager.multiplexer.adapter import ControllerIOAdapter
from services.controller_manager.multiplexer.motion_patterns import POLICY_THRESHOLD, Archetype, SignalHasher

logger = logging.getLogger(__name__)


@dataclass
class PatternRuntime:
    """Per-controller state of an installed motion pattern (UC-046).

    ``frame_index`` is k of BR-001: it advances once per frame DELIVERED on the
    owning gameplay stream (the first stream whose explicit filter names this
    controller), never per 100 Hz poll and never on other streams, so the signal
    of the game's stream is f(0), f(1/r), f(2/r), ... whatever the host load.
    """

    archetype: Archetype
    slot: int
    frame_index: int = 0
    owner: str | None = None
    overruns: int = 0
    last_value: float = 1.0
    hasher: SignalHasher = field(init=False)
    # Threshold policy lookback (UC-046 BR-018): the newest frame j scanned and the
    # newest j whose intensity reached the level, so each frame is evaluated once.
    _scanned_to: int = field(default=-1, init=False)
    _last_hit: int = field(default=-(10**9), init=False)

    def __post_init__(self) -> None:
        self.hasher = SignalHasher(self.archetype.stream_rate_hz)

    def trigger(self, k: int) -> bool:
        """``archetype.trigger(slot, k)`` for a non-decreasing k, in O(1) amortised per frame.

        k only grows (it advances once per owned frame, also across a reconnect),
        so a threshold lookback scans every frame j at most once.
        """
        policy = self.archetype.trigger_policy
        if policy is None:
            return False
        if policy.kind != POLICY_THRESHOLD:
            return self.archetype.trigger(self.slot, k)
        newest = k - policy.reaction_frames
        oldest = max(0, newest - policy.hold_frames + 1)
        for j in range(max(oldest, self._scanned_to + 1), newest + 1):
            if self.archetype.value(self.slot, j) >= policy.level:
                self._last_hit = j
        self._scanned_to = max(self._scanned_to, newest)
        return self._last_hit >= oldest and newest >= 0

    def record(self) -> dict:
        """The replay record of UC-046 step 8 (RemoveController / pattern_release)."""
        return {
            "archetype": self.archetype.name,
            "slot": self.slot,
            "frames_delivered": self.hasher.frames,
            "signal_hash": self.hasher.value,
            "signal_checkpoints": list(self.hasher.checkpoints),
            "frame_overruns": self.overruns,
        }


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
        # A trigger press since the last delivered game frame (#1837): set by
        # SimulateButton, cleared by consume_trigger_press, so a press shorter than
        # the frame interval still reaches the game once.
        "trigger_latched": False,
        # Number of gameplay-stream frames the death-level acceleration must
        # still be DELIVERED in. Decremented by the gameplay send path
        # (consume_death_frame), NOT by a wall-clock timer — see #926. A
        # wall-clock latch could be skipped entirely by a send loop starved
        # under parallel-game CPU load, so the death never reached the game
        # and the session never converged. Counting delivered frames makes the
        # death cadence-independent: it is guaranteed to cross the EMA death
        # threshold regardless of how slowly frames are streamed, and the small
        # budget (2) leaves at most one trailing frame after the kill — which
        # lands well inside the 2.0s respawn grace (EMA reset) — so no death
        # frame survives grace to trigger a #757 re-kill.
        "death_frames_remaining": 0,
        # Generous wall-clock fallback: clears a death latch ONLY for a
        # controller no gameplay stream is draining (a stray SimulateDeath on a
        # controller not in a running game), so it never sticks forever. It is
        # set far above any in-game delivery time, so in a real game the frame
        # budget — not this timer — governs the latch (#926).
        "death_hold_until": 0.0,
        # Acceleration held by SimulateMovement (UC-003 BR-002, #1279). While
        # set, poll() reports exactly this vector instead of resting noise, so a
        # scripted value reaches every streamed gameplay frame until the next
        # SimulateMovement or ResetController. None = unscripted (pure noise).
        "held_accel": None,
        # Installed motion pattern (UC-046, #1258). While set, the gameplay send
        # path delivers intensity(k / r) instead of the polled acceleration (BR-012).
        "pattern": None,
    }


def _put_dropping_when_full(queue: asyncio.Queue, event: dict) -> None:
    """put_nowait that drops the event when the observer queue is full."""
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(event)


class MockAdapter(ControllerIOAdapter):
    """Simulated I/O adapter for testing without hardware.

    Provides the ControllerIOAdapter interface plus extra methods
    needed by MockControllerService (add_controller, remove_controller,
    add_observer, remove_observer, get_led_color, controllers property).
    """

    def __init__(self, num_controllers: int = 0):
        self._num_controllers = num_controllers
        self.controllers: dict[str, dict] = {}
        # Each observer queue with the loop that owns it (None when created
        # outside a running loop). set_output runs in the 20 Hz LED worker
        # thread, and asyncio.Queue is not thread-safe (#1478).
        self._observers: list[tuple[asyncio.Queue, asyncio.AbstractEventLoop | None]] = []
        # Serialises the copy-and-replace in add/remove_observer, so two loops
        # adding at once cannot drop each other's queue (#1630). Readers
        # (_publish_event) iterate the current list without it.
        self._observers_lock = threading.Lock()
        # Monotonic serial counter for auto-generated serials. It NEVER decreases,
        # even when controllers are removed, so a freshly added controller can never
        # collide with one whose removal is still in flight (issue #1013): the old
        # len()-based scheme reused MOCK000N after a removal, so a new AddControllers
        # could hand back a serial already held by a not-yet-removed prior game,
        # collapsing two distinct players to one serial and aborting FFA with
        # "Need at least 2 players, got 1".
        self._next_serial = 0
        # Serials carrying a motion pattern (UC-046): lets the gameplay send path
        # skip pattern work entirely for every other controller.
        self.pattern_serials: set[str] = set()
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
        # game enough times to cross the EMA death threshold (#926). The frame
        # budget (death_frames_remaining) is decremented by the gameplay send
        # path (consume_death_frame), not here — poll() only reports the current
        # latch state. In a game the budget is the ONLY thing that clears the
        # latch: there is no early wall-clock cut, so a starved send loop can
        # never skip the death. The wall-clock value is a generous fallback that
        # clears a latch nothing is draining (controller not in a game); it is
        # set far above any in-game delivery time, so it never cuts a real death
        # short.
        death_accel = None
        death_active = controller["death_accel"] and (
            controller["death_frames_remaining"] > 0
            and (controller["death_hold_until"] == 0.0 or controller["death_hold_until"] > current_time)
        )
        if death_active:
            death_accel = controller["death_accel"]
        else:
            if controller["death_accel"] is not None:
                # Latch cleared (budget drained, or idle fallback passed): revert.
                controller["death_accel"] = None
                controller["death_frames_remaining"] = 0
                controller["death_hold_until"] = 0.0

            held_accel = controller["held_accel"]
            pattern = controller["pattern"]
            if pattern is not None:
                # UC-046 BR-012: a pattern controller never reports a SimulateMovement
                # hold or noise. Delivered frames come from pattern_frame(); the polled
                # state mirrors the last delivered value for other readers.
                controller[StateKey.ACCEL] = {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: pattern.last_value}
            elif held_accel is not None:
                # Scripted motion persists until changed (#1279).
                controller[StateKey.ACCEL] = dict(held_accel)
            else:
                # Unscripted: resting noise around 1 g. Built whole and assigned
                # once so a concurrent reader never sees a half-updated frame.
                controller[StateKey.ACCEL] = {
                    AxisKey.X: random.gauss(0.0, 0.1),
                    AxisKey.Y: random.gauss(0.0, 0.1),
                    AxisKey.Z: random.gauss(1.0, 0.1),
                }

        # Gyroscope noise, assigned once for the same reason.
        controller[StateKey.GYRO] = {
            AxisKey.X: random.gauss(0.0, 0.5),
            AxisKey.Y: random.gauss(0.0, 0.5),
            AxisKey.Z: random.gauss(0.0, 0.5),
        }

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

    def hold_accel(self, serial: str, x: float, y: float, z: float) -> bool:
        """Hold an acceleration vector on a controller until changed (#1279).

        Ignored by delivery for a pattern controller: the pattern wins (UC-046 BR-012).

        Every subsequent ``poll()`` reports exactly this vector (instead of
        resting noise) until the next ``hold_accel`` or ``clear_held_accel``.
        An active death latch still wins for its delivered frames; the held
        vector resumes once the latch drains. Returns False for an unknown
        controller.

        Raises:
            ValueError: if any component is NaN or infinite.
        """
        if not all(math.isfinite(v) for v in (x, y, z)):
            raise ValueError(f"acceleration must be finite, got ({x}, {y}, {z})")
        controller = self.controllers.get(serial)
        if not controller:
            return False
        held = {AxisKey.X: x, AxisKey.Y: y, AxisKey.Z: z}
        controller["held_accel"] = held
        controller[StateKey.ACCEL] = dict(held)
        return True

    def clear_held_accel(self, serial: str) -> bool:
        """Drop a held acceleration so the controller reports resting noise again."""
        controller = self.controllers.get(serial)
        if not controller:
            return False
        controller["held_accel"] = None
        return True

    # -- Motion patterns (UC-046, #1258) --

    def install_pattern(self, serials: list[str], archetype: Archetype) -> None:
        """Bind slot i of ``archetype`` to ``serials[i]`` (UC-046 step 4, BR-006)."""
        if len(serials) != archetype.players:
            raise ValueError(f"{archetype.name} has {archetype.players} players, got {len(serials)} serials")
        for slot, serial in enumerate(serials):
            controller = self.controllers[serial]
            controller["pattern"] = PatternRuntime(archetype=archetype, slot=slot)
            self.pattern_serials.add(serial)
            controller["held_accel"] = None

    def has_pattern(self, serial: str) -> bool:
        controller = self.controllers.get(serial)
        return bool(controller and controller.get("pattern") is not None)

    def pattern_info(self, serial: str) -> PatternRuntime | None:
        controller = self.controllers.get(serial)
        return controller.get("pattern") if controller else None

    def pattern_frame(self, serial: str, subscriber_id: str, explicit: bool) -> tuple[dict, bool] | None:
        """Acceleration and trigger to DELIVER for a pattern controller on one stream frame.

        Returns None for a controller without a pattern (the caller keeps the
        polled state), else ``(accel, trigger)``. The trigger is the archetype's
        policy at frame k (UC-046 BR-018), taken from k only, never from the
        delivered value (a death latch does not change it); it is True only on the
        owning stream, so an observer never sees a press. The first stream that names the controller in its explicit
        filter becomes its owner; only the owner advances k (UC-046 BR-001), so a
        dashboard stream (no filter) never perturbs the game's signal and just sees
        the last delivered value. Precedence (BR-012): an active SimulateDeath latch
        is delivered first, then intensity(k / r); k advances on every owned frame and the
        signal hash covers the delivered magnitude.
        """
        controller = self.controllers.get(serial)
        if not controller:
            return None
        pattern: PatternRuntime | None = controller.get("pattern")
        if pattern is None:
            return None
        if pattern.owner is None and explicit:
            pattern.owner = subscriber_id
        if pattern.owner != subscriber_id:
            return {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: pattern.last_value}, False

        death = controller["death_accel"]
        if death and controller["death_frames_remaining"] > 0:
            accel = dict(death)
            value = math.sqrt(sum(v * v for v in death.values()))
        else:
            value = pattern.archetype.value(pattern.slot, pattern.frame_index)
            accel = {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: value}
        has_policy = pattern.archetype.has_trigger_policy
        trigger = pattern.trigger(pattern.frame_index) if has_policy else False
        pattern.hasher.update(value, trigger if has_policy else None)
        pattern.frame_index += 1
        pattern.last_value = value
        return accel, trigger

    def pattern_stream_rate(self, serials) -> int | None:
        """Pinned rate of a stream naming pattern controllers (UC-046 FR-103), else None."""
        for serial in serials:
            pattern = self.pattern_info(serial)
            if pattern is not None:
                return pattern.archetype.stream_rate_hz
        return None

    def record_pattern_overrun(self, subscriber_id: str) -> int:
        """Count a frame overrun for every pattern controller this stream owns (UC-046 A10)."""
        counted = 0
        for controller in self.controllers.values():
            pattern = controller.get("pattern")
            if pattern is not None and pattern.owner == subscriber_id:
                pattern.overruns += 1
                counted += 1
        return counted

    def release_pattern_stream(self, subscriber_id: str) -> None:
        """A stream ended: its controllers may be claimed by the next explicit stream (reconnect)."""
        for controller in self.controllers.values():
            pattern = controller.get("pattern")
            if pattern is not None and pattern.owner == subscriber_id:
                pattern.owner = None

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

    def latch_trigger_press(self, serial: str) -> bool:
        """Remember a trigger press until the next delivered game frame (#1837)."""
        controller = self.controllers.get(serial)
        if not controller:
            return False
        controller["trigger_latched"] = True
        return True

    def consume_trigger_press(self, serial: str) -> bool:
        """True when the trigger was pressed since the last call, then clear (#1837)."""
        controller = self.controllers.get(serial)
        if not controller or not controller["trigger_latched"]:
            return False
        controller["trigger_latched"] = False
        return True

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
        self.pattern_serials.discard(serial)
        if serial in self.controllers:
            del self.controllers[serial]
            logger.info(f"Mock: Closed controller {serial}")

    def close_all(self) -> None:
        """Remove all controllers."""
        self.controllers.clear()
        self.pattern_serials.clear()

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
            # Use a monotonic counter (never reused after removal) so an
            # auto-generated serial is globally unique for the adapter's lifetime
            # and cannot collide with a controller whose removal is still in flight
            # (#1013). Skip any serial that somehow already exists (e.g. a caller
            # passed an explicit "MOCK000N" earlier) to keep the guarantee absolute.
            serial = f"MOCK{self._next_serial:04d}"
            self._next_serial += 1
            while serial in self.controllers:
                serial = f"MOCK{self._next_serial:04d}"
                self._next_serial += 1
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
        self.pattern_serials.discard(serial)
        if serial in self.controllers:
            del self.controllers[serial]
            logger.info(f"Mock: Removed controller {serial}")
            return True
        return False

    def add_observer(self) -> asyncio.Queue:
        """Add an observer queue for state changes, bound to the running loop (if any)."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        with self._observers_lock:
            self._observers = [*self._observers, (queue, loop)]
        logger.debug(f"Added observer, total: {len(self._observers)}")
        return queue

    def remove_observer(self, queue: asyncio.Queue) -> None:
        """Remove an observer queue."""
        with self._observers_lock:
            remaining = [entry for entry in self._observers if entry[0] is not queue]
            removed = len(remaining) != len(self._observers)
            if removed:
                self._observers = remaining
        if removed:
            logger.debug(f"Removed observer, remaining: {len(self._observers)}")

    def get_led_color(self, serial: str) -> tuple[int, int, int] | None:
        """Get current LED color for a controller."""
        controller = self.controllers.get(serial)
        if not controller:
            return None
        led = controller.get("led", {"r": 0, "g": 0, "b": 0})
        return (led["r"], led["g"], led["b"])

    def _publish_event(self, event: dict) -> None:
        """Publish event to all observers.

        Called from the event loop and from the LED worker thread. The put is
        always scheduled on the queue's own loop: call_soon from that loop,
        call_soon_threadsafe from any other thread (#1478). Both append to the
        same ready queue, so events keep their publish order.
        """
        try:
            current: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        # The list is replaced (never mutated) by add/remove_observer, so this
        # iterates a stable snapshot even when another thread changes it.
        for queue, loop in self._observers:
            if loop is None:
                _put_dropping_when_full(queue, event)
            elif loop is current:
                # Also scheduled (not put directly) so an event published here
                # cannot overtake one a worker thread queued just before it:
                # observers see events in publish order (#1629 review).
                loop.call_soon(_put_dropping_when_full, queue, event)
            else:
                # The owning loop may already be closed (stream ended mid-batch).
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(_put_dropping_when_full, queue, event)
