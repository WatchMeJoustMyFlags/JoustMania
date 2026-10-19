"""
Mock Controller Control Service for integration testing.

Provides control RPCs for simulating controller behavior during tests.
Only active when using MockAdapter.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import grpc
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from lib.controller_constants import AxisKey, ButtonKey, StateKey
from proto import controller_manager_mock_pb2, controller_manager_mock_pb2_grpc
from services.controller_manager.multiplexer.motion_patterns import validate_archetype

if TYPE_CHECKING:
    from services.controller_manager.multiplexer.mock_adapter import MockAdapter

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# UC-046 BR-004: scripted motion is refused for pattern controllers.
PATTERN_REFUSAL = "pattern controller: deaths are organic only (UC-046 BR-004)"

# Number of gameplay-stream frames the death-level acceleration is guaranteed
# to be DELIVERED to the game (#926). The death latch is counted in DELIVERED
# frames, not wall-clock: the gameplay send path drains exactly one frame per
# streamed frame (consume_death_frame), so a send loop starved under
# parallel-game CPU load merely spreads these frames over more wall-clock — it
# can no longer skip the death window entirely (the old wall-clock-only hold
# could be skipped wholesale between two starved sends, so the death never
# reached the game and the session stalled with all players alive).
#
# Why exactly 2 — minimum-with-margin for the EMA crossing AND #757-safe by
# construction (this is the value the rework settled on after working both
# failure modes through the real numbers; the original PR used 8, which broke
# both — see the PR discussion / #926 review):
#
#   Crossing the death threshold. The game smooths streamed acceleration with an
#   EMA, weight 4 (smoothed = (smoothed*4 + raw)/5), and kills when it crosses
#   the per-sensitivity death threshold. The mock can face ANY sensitivity (0..4)
#   at EITHER music tempo — the integration flagd config targets Werewolf to the
#   "fast" profile (sensitivity 3, death threshold up to 3.2g) and other modes
#   reach sensitivity 4 (3.2g slow / 3.5g fast). The highest threshold the mock
#   can be asked to cross is therefore 3.5g.
#
#   The death input must be sized so that 2 DELIVERED death frames cross that
#   3.5g ceiling from a resting-primed EMA (~1.0g). With budget 2 the EMA after
#   the second delivered death frame is (16 + 9*raw)/25, so raw >= ~7.95g is
#   required for frame 2 >= 3.5g. We use ~9.95g (axes 7,5,5) for comfortable
#   margin. From rest (EMA ~1.0g) the EMA then climbs:
#       frame 1 -> 2.79g     frame 2 -> 4.22g
#   Per-sensitivity kill frame (the frame whose EMA first exceeds the threshold),
#   verified against the full SLOW_MAX/FAST_MAX tables in
#   services/game_coordinator/games/base.py:
#       sens | slow_max kill | fast_max kill
#         0  |  1.3g  frame 1 |  1.6g  frame 1
#         1  |  1.5g  frame 1 |  1.8g  frame 1
#         2  |  1.8g  frame 1 |  2.8g  frame 2
#         3  |  2.5g  frame 1 |  3.2g  frame 2
#         4  |  3.2g  frame 2 |  3.5g  frame 2
#   Every sensitivity at both tempos crosses by frame 2 at the latest, so a
#   budget of 2 reliably kills across the WHOLE table. (The earlier 7.07g input
#   only reached 3.19g on frame 2 and silently FAILED to kill sensitivity 3-fast
#   and sensitivity 4 — the gap this #926 re-review caught; the suite papered
#   over it only because kill_player_verified re-fires until a slow-tempo window
#   is caught.)
#
#   #757-safety by construction. The game-side kill fires on the death frame
#   whose EMA first crosses the threshold (frame 1 or frame 2 per the table
#   above); the killed player is immediately marked not-alive and given a 2.0s
#   respawn grace during which the game RESETS its EMA every frame. At most
#   (budget - kill_frame) death frames are streamed AFTER the killing frame, and
#   the latch then EXHAUSTS. The worst case is a frame-1 kill (low sensitivity),
#   leaving exactly ONE trailing death frame; the frame-2 kills (sensitivity
#   3-fast / 4) leave ZERO trailing frames. That single trailing frame lands one
#   send interval S after the kill, where S is the send interval. For it to re-prime
#   the EMA past grace it would have to arrive after grace_until — i.e. need
#   1 * S >= 2.0s. Even at a heavily starved ~1s send gap (the PR's own
#   root-cause cadence) it lands at ~1.0s, comfortably inside the 2.0s grace
#   where the EMA is reset, so it is harmless. The larger the budget, the more
#   trailing frames and the sooner one escapes grace: budget 3 already re-kills
#   at a ~1.05s gap, and budget 8 (the original PR) re-kills almost immediately
#   under starvation (the #757 regression the review flagged). The SMALL budget,
#   not a wall-clock cap, is what makes this safe — so there is NO early
#   wall-clock cut on the in-game path (#926).
DEATH_DELIVERY_FRAMES = 2

# Generous wall-clock fallback that clears a death latch ONLY for a controller
# with no gameplay consumer draining it (a stray SimulateDeath on a controller
# that is not in a running game). It is deliberately set FAR above any in-game
# delivery time — well above the 2.0s respawn grace and any realistic starved
# send gap — so it can NEVER fire before the death frames are delivered to a
# controller that IS in a game. In-game, the frame budget alone governs the
# latch (no early wall-clock cut, so a starved send loop can never skip the
# death — #926); the wall-clock fallback matters only when the budget is not
# draining because nothing is streaming this controller.
DEATH_IDLE_FALLBACK_SECONDS = 30.0


class MockControllerService(controller_manager_mock_pb2_grpc.MockControllerServiceServicer):
    """Service for controlling mock controllers during integration tests."""

    def __init__(self, backend: "MockAdapter"):
        """
        Initialize mock control service.

        Args:
            backend: MockAdapter instance to control
        """
        self.backend = backend
        logger.info("MockControllerService initialized")

    def SimulateMovement(self, request, context):
        """Hold an acceleration vector on a controller until changed or reset.

        The value persists across polls (UC-003 BR-002, #1279): every poll and
        so every streamed gameplay frame reports it until the next
        SimulateMovement or ResetController. A SimulateDeath latch overrides it
        for its delivered frames, after which the held value resumes. A NaN or
        infinite component is rejected with INVALID_ARGUMENT.
        """
        try:
            serial = request.serial
            if self.backend.has_pattern(serial):
                if context is not None:
                    context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
                    context.set_details(PATTERN_REFUSAL)
                return controller_manager_mock_pb2.MovementResponse(success=False, error=PATTERN_REFUSAL)
            try:
                held = self.backend.hold_accel(serial, request.accel_x, request.accel_y, request.accel_z)
            except ValueError as e:
                if context is not None:
                    context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                    context.set_details(str(e))
                return controller_manager_mock_pb2.MovementResponse(success=False, error=str(e))
            if not held:
                return controller_manager_mock_pb2.MovementResponse(
                    success=False, error=f"Controller {serial} not found"
                )

            logger.info(
                f"Mock: Holding accel for {serial}: "
                f"({request.accel_x:.2f}, {request.accel_y:.2f}, {request.accel_z:.2f})"
            )
            return controller_manager_mock_pb2.MovementResponse(success=True, error="")

        except Exception as e:
            logger.error(f"SimulateMovement error: {e}")
            return controller_manager_mock_pb2.MovementResponse(success=False, error=str(e))

    def SimulateDeath(self, request, context):
        """Simulate death by setting high acceleration and holding it briefly."""
        try:
            serial = request.serial
            if serial not in self.backend.controllers:
                return controller_manager_mock_pb2.DeathResponse(success=False, accel_magnitude=0.0)
            if self.backend.has_pattern(serial):
                if context is not None:
                    context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
                    context.set_details(PATTERN_REFUSAL)
                return controller_manager_mock_pb2.DeathResponse(success=False, accel_magnitude=0.0)

            # UC-003 BR-001: latch about 9.95 g for DEATH_DELIVERY_FRAMES (2) delivered frames.
            # Set death-level acceleration. Sized (~9.95g) so that 2 DELIVERED
            # death frames cross the HIGHEST per-sensitivity death threshold the
            # mock can face (sensitivity 4 fast = 3.5g) from a resting-primed EMA
            # — see DEATH_DELIVERY_FRAMES for the full per-sensitivity table.
            death_accel = {AxisKey.X: 7.0, AxisKey.Y: 5.0, AxisKey.Z: 5.0}
            accel_mag = (7.0**2 + 5.0**2 + 5.0**2) ** 0.5  # ~9.95g

            # Latch the death by a DELIVERED-frame budget, not wall-clock (#926):
            # the gameplay send path decrements it once per streamed frame, so
            # the death-level acceleration is guaranteed to reach the game in
            # DEATH_DELIVERY_FRAMES frames — enough to cross the EMA death
            # threshold — regardless of how starved the send loop is under
            # parallel-game CPU load. In-game the budget is the ONLY thing that
            # clears the latch; the wall-clock value is a generous fallback that
            # fires only for a controller no gameplay stream is draining (see
            # DEATH_IDLE_FALLBACK_SECONDS), so it can never cut the death short
            # in a real game.
            controller = self.backend.controllers[serial]
            controller["death_accel"] = death_accel
            controller["death_frames_remaining"] = DEATH_DELIVERY_FRAMES
            controller["death_hold_until"] = time.time() + DEATH_IDLE_FALLBACK_SECONDS

            logger.info(
                f"Mock: Simulated death for {serial} with {accel_mag:.2f}g acceleration, "
                f"holding for {DEATH_DELIVERY_FRAMES} delivered frames "
                f"(idle fallback {DEATH_IDLE_FALLBACK_SECONDS}s)"
            )
            return controller_manager_mock_pb2.DeathResponse(success=True, accel_magnitude=accel_mag)

        except Exception as e:
            logger.error(f"SimulateDeath error: {e}")
            return controller_manager_mock_pb2.DeathResponse(success=False, accel_magnitude=0.0)

    def SimulateButton(self, request, _context):
        """Simulate button press."""
        try:
            serial = request.serial
            if serial not in self.backend.controllers:
                return controller_manager_mock_pb2.ButtonResponse(success=False, error=f"Controller {serial} not found")

            # Map proto button enum to backend state keys
            button_map = {
                controller_manager_mock_pb2.ButtonRequest.TRIGGER: ButtonKey.TRIGGER,
                controller_manager_mock_pb2.ButtonRequest.MOVE: ButtonKey.MOVE,
                controller_manager_mock_pb2.ButtonRequest.SELECT: ButtonKey.SELECT,
                controller_manager_mock_pb2.ButtonRequest.START: ButtonKey.START,
                controller_manager_mock_pb2.ButtonRequest.CROSS: ButtonKey.CROSS,
                controller_manager_mock_pb2.ButtonRequest.CIRCLE: ButtonKey.CIRCLE,
                controller_manager_mock_pb2.ButtonRequest.SQUARE: ButtonKey.SQUARE,
                controller_manager_mock_pb2.ButtonRequest.TRIANGLE: ButtonKey.TRIANGLE,
                controller_manager_mock_pb2.ButtonRequest.PS: ButtonKey.PS,
            }

            button_key = button_map.get(request.button)
            if button_key is None:
                return controller_manager_mock_pb2.ButtonResponse(
                    success=False, error=f"Unknown button: {request.button}"
                )

            # Set button state
            self.backend.controllers[serial][button_key] = request.pressed

            logger.info(f"Mock: Set {button_key} on {serial} to {request.pressed}")
            return controller_manager_mock_pb2.ButtonResponse(success=True, error="")

        except Exception as e:
            logger.error(f"SimulateButton error: {e}")
            return controller_manager_mock_pb2.ButtonResponse(success=False, error=str(e))

    def SetColor(self, request, _context):
        """Set controller LED color."""
        try:
            serial = request.serial
            if serial not in self.backend.controllers:
                return controller_manager_mock_pb2.ColorResponse(success=False, error=f"Controller {serial} not found")

            self.backend.controllers[serial]["led"] = {"r": request.r, "g": request.g, "b": request.b}

            logger.info(f"Mock: Set LED for {serial} to RGB({request.r}, {request.g}, {request.b})")
            return controller_manager_mock_pb2.ColorResponse(success=True, error="")

        except Exception as e:
            logger.error(f"SetColor error: {e}")
            return controller_manager_mock_pb2.ColorResponse(success=False, error=str(e))

    def ResetController(self, request, _context):
        """Reset controller to idle state."""
        try:
            serial = request.serial
            if serial not in self.backend.controllers:
                return controller_manager_mock_pb2.ResetResponse(success=False, error=f"Controller {serial} not found")

            # Reset to idle - all buttons released
            controller = self.backend.controllers[serial]
            controller[ButtonKey.MOVE] = False
            controller[ButtonKey.TRIGGER] = False
            controller[ButtonKey.PS] = False
            controller[ButtonKey.SELECT] = False
            controller[ButtonKey.START] = False
            controller[ButtonKey.TRIANGLE] = False
            controller[ButtonKey.CIRCLE] = False
            controller[ButtonKey.CROSS] = False
            controller[ButtonKey.SQUARE] = False
            controller[StateKey.ACCEL] = {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 1.0}  # At rest
            controller[StateKey.GYRO] = {AxisKey.X: 0.0, AxisKey.Y: 0.0, AxisKey.Z: 0.0}
            # Drop any SimulateMovement hold: back to resting noise (UC-003 A3)
            self.backend.clear_held_accel(serial)
            # Clear death state
            controller["death_accel"] = None
            controller["death_frames_remaining"] = 0
            controller["death_hold_until"] = 0.0

            logger.info(f"Mock: Reset {serial} to idle state")
            return controller_manager_mock_pb2.ResetResponse(success=True, error="")

        except Exception as e:
            logger.error(f"ResetController error: {e}")
            return controller_manager_mock_pb2.ResetResponse(success=False, error=str(e))

    def ListMockControllers(self, _request, _context):
        """List all mock controllers with reserved/tag metadata.

        The repeated ``controllers`` field lets tests/agents enumerate their
        reserved controllers and sweep orphans by tag. The legacy ``serials``
        field is kept for backward compatibility.
        """
        try:
            serials = list(self.backend.controllers.keys())
            infos = [
                controller_manager_mock_pb2.MockControllerInfo(
                    serial=serial,
                    reserved=state.get("reserved", False),
                    tag=state.get("tag", ""),
                )
                for serial, state in self.backend.controllers.items()
            ]
            return controller_manager_mock_pb2.ListResponse(serials=serials, count=len(serials), controllers=infos)

        except Exception as e:
            logger.error(f"ListMockControllers error: {e}")
            return controller_manager_mock_pb2.ListResponse(serials=[], count=0)

    def GetColor(self, request, _context):
        """Get current LED color for a controller."""
        try:
            serial = request.serial
            color = self.backend.get_led_color(serial)
            if color is None:
                return controller_manager_mock_pb2.GetColorResponse(
                    success=False, error=f"Controller {serial} not found"
                )

            r, g, b = color
            logger.debug(f"Mock: GetColor for {serial}: RGB({r}, {g}, {b})")
            return controller_manager_mock_pb2.GetColorResponse(success=True, r=r, g=g, b=b)

        except Exception as e:
            logger.error(f"GetColor error: {e}")
            return controller_manager_mock_pb2.GetColorResponse(success=False, error=str(e))

    def AddController(self, request, _context):
        """Add a single mock controller dynamically.

        When request.reserved is set, the controller is hidden from
        button-stream consumers (the menu); request.tag identifies the owner.
        """
        try:
            serial = request.serial if request.serial else None
            added_serial = self.backend.add_controller(serial, reserved=request.reserved, tag=request.tag)
            logger.info(f"Mock: Added controller {added_serial} (reserved={request.reserved}, tag={request.tag!r})")
            return controller_manager_mock_pb2.AddControllerResponse(success=True, serial=added_serial)
        except Exception as e:
            logger.error(f"AddController error: {e}")
            return controller_manager_mock_pb2.AddControllerResponse(success=False, error=str(e), serial="")

    def RemoveController(self, request, _context):
        """Remove a mock controller; a pattern controller returns its replay record (UC-046 step 8)."""
        try:
            serial = request.serial
            pattern = self.backend.pattern_info(serial)
            removed = self.backend.remove_controller(serial)
            if removed:
                logger.info(f"Mock: Removed controller {serial}")
                if pattern is not None:
                    return self._pattern_release(serial, pattern.record())
                return controller_manager_mock_pb2.RemoveControllerResponse(success=True)
            return controller_manager_mock_pb2.RemoveControllerResponse(
                success=False, error=f"Controller {serial} not found"
            )
        except Exception as e:
            logger.error(f"RemoveController error: {e}")
            return controller_manager_mock_pb2.RemoveControllerResponse(success=False, error=str(e))

    def AddControllers(self, request, _context):
        """Add multiple mock controllers at once.

        request.reserved/request.tag apply to every added controller. With
        request.archetype the agent-pinned parameters are validated and slot i is
        installed on the i-th returned serial (UC-046 step 4); invalid parameters
        add nothing. The controller manager never reads flagd for archetypes.
        """
        if request.archetype:
            return self._add_pattern_controllers(request)
        try:
            serials = []
            for _ in range(request.count):
                serial = self.backend.add_controller(reserved=request.reserved, tag=request.tag)
                serials.append(serial)
            logger.info(
                f"Mock: Added {request.count} controllers: {serials} (reserved={request.reserved}, tag={request.tag!r})"
            )
            return controller_manager_mock_pb2.AddControllersResponse(success=True, serials=serials)
        except Exception as e:
            logger.error(f"AddControllers error: {e}")
            return controller_manager_mock_pb2.AddControllersResponse(success=False, error=str(e))

    async def StreamObservability(self, _request, context):
        """Stream observable events from mock controllers (LED changes, button presses, etc.)."""
        queue = self.backend.add_observer()
        logger.info("Mock: Started observability stream")

        try:
            while not context.cancelled():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield self._event_to_proto(event)
                except TimeoutError:
                    continue
        finally:
            self.backend.remove_observer(queue)
            logger.info("Mock: Ended observability stream")

    def _event_to_proto(self, event: dict) -> controller_manager_mock_pb2.ObservabilityEvent:
        """Convert internal event dict to protobuf message."""
        proto_event = controller_manager_mock_pb2.ObservabilityEvent(
            timestamp_ms=event.get("timestamp_ms", 0),
            serial=event.get("serial", ""),
        )

        match event.get("type"):
            case "led_change":
                proto_event.led_change.CopyFrom(
                    controller_manager_mock_pb2.LedChangeEvent(
                        r=event.get("r", 0),
                        g=event.get("g", 0),
                        b=event.get("b", 0),
                        source=event.get("source", ""),
                    )
                )
            case "rumble_change":
                proto_event.rumble_change.CopyFrom(
                    controller_manager_mock_pb2.RumbleChangeEvent(
                        intensity=event.get("intensity", 0),
                    )
                )
            case "button_change":
                proto_event.button_change.CopyFrom(
                    controller_manager_mock_pb2.ButtonChangeEvent(
                        button=event.get("button", ""),
                        pressed=event.get("pressed", False),
                    )
                )
            case "connection":
                proto_event.connection.CopyFrom(
                    controller_manager_mock_pb2.ConnectionEvent(
                        connected=event.get("connected", False),
                    )
                )

        return proto_event

    # ------------------------------------------------------------------
    # Motion patterns (UC-046, #1258)
    # ------------------------------------------------------------------

    def _add_pattern_controllers(self, request):
        """AddControllers with an archetype: validate, reserve one controller per player, install.

        Pattern controllers are always reserved (hidden from the lobby, UC-046
        Post-F-2), whatever request.reserved says.
        """
        try:
            return self._install_pattern(request)
        except Exception as e:
            logger.error(f"AddControllers (archetype) error: {e}", exc_info=True)
            return controller_manager_mock_pb2.AddControllersResponse(success=False, error=str(e))

    def _install_pattern(self, request):
        with tracer.start_as_current_span("controller.mock.pattern_install") as span:
            span.set_attribute("pattern.archetype", request.archetype)
            span.set_attribute("mock.tag", request.tag)
            archetype, errors = validate_archetype(request.archetype, request.archetype_params)
            if archetype is None:
                span.set_attribute("pattern.validation_errors", errors)
                span.set_status(Status(StatusCode.ERROR, "invalid archetype"))
                error = f"archetype {request.archetype!r} rejected: " + "; ".join(errors)
                return controller_manager_mock_pb2.AddControllersResponse(success=False, error=error)
            span.set_attribute("pattern.stream_rate_hz", archetype.stream_rate_hz)
            span.set_attribute("pattern.players", archetype.players)
            # The agent's own canonical JSON (its params digest is computed over it).
            span.set_attribute("pattern.params", request.archetype_params)
            if request.count != archetype.players:
                error = f"count {request.count} != players {archetype.players} of {archetype.name}"
                span.set_status(Status(StatusCode.ERROR, error))
                return controller_manager_mock_pb2.AddControllersResponse(success=False, error=error)
            serials = [self.backend.add_controller(reserved=True, tag=request.tag) for _ in range(archetype.players)]
            self.backend.install_pattern(serials, archetype)
            for slot, serial in enumerate(serials):
                span.add_event("pattern.slot", {"pattern.slot_index": slot, "controller.serial": serial})
            logger.info(f"Mock: Added pattern controllers {serials} for {archetype.name} (tag={request.tag!r})")
            return controller_manager_mock_pb2.AddControllersResponse(
                success=True,
                serials=serials,
                archetype=archetype.name,
                stream_rate_hz=archetype.stream_rate_hz,
                players=archetype.players,
            )

    def _pattern_release(self, serial: str, record: dict):
        """Emit controller.mock.pattern_release and return the replay record."""
        with tracer.start_as_current_span("controller.mock.pattern_release") as span:
            span.set_attribute("controller.serial", serial)
            span.set_attribute("pattern.archetype", record["archetype"])
            span.set_attribute("pattern.slot_index", record["slot"])
            span.set_attribute("pattern.frames_delivered", record["frames_delivered"])
            span.set_attribute("pattern.signal_hash", f"{record['signal_hash']:016x}")
            span.set_attribute("pattern.signal_checkpoints", record["signal_checkpoints"])
            span.set_attribute("pattern.frame_overruns", record["frame_overruns"])
        return controller_manager_mock_pb2.RemoveControllerResponse(
            success=True,
            pattern_slot=record["slot"],
            frames_delivered=record["frames_delivered"],
            signal_hash=record["signal_hash"],
            frame_overruns=record["frame_overruns"],
            signal_checkpoints=record["signal_checkpoints"],
            archetype=record["archetype"],
        )
