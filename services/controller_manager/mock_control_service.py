"""
Mock Controller Control Service for integration testing.

Provides control RPCs for simulating controller behavior during tests.
Only active when using MockAdapter.
"""

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING

from lib.controller_constants import AxisKey, ButtonKey, StateKey
from proto import controller_manager_mock_pb2, controller_manager_mock_pb2_grpc

if TYPE_CHECKING:
    from services.controller_manager.multiplexer.mock_adapter import MockAdapter

logger = logging.getLogger(__name__)

# How long SimulateDeath holds death-level acceleration. Long enough for the
# game loop to reliably detect it (>= 60 frames at 60Hz), but well below the
# 2.0s post-swap/respawn grace period — a hold that outlives grace re-kills
# the player the moment grace expires (#757).
#
# NOTE: This wall-clock value is now only a SAFETY CAP for controllers that are
# not in a game (no gameplay stream draining the latch). The real death latch
# is frame-budget based (DEATH_DELIVERY_FRAMES) and is decremented by the
# gameplay send path — see SimulateDeath / mock_adapter.consume_death_frame and
# #926.
DEATH_HOLD_SECONDS = 1.0

# Number of gameplay-stream frames the death-level acceleration is guaranteed
# to be DELIVERED in (#926). The game's EMA filter (weight 4) crosses the death
# threshold after a single ~7g frame, so a handful of delivered frames is
# decisive while still draining well inside the 2.0s post-respawn grace window
# (the send path decrements the budget every frame, including during grace, so
# the latch is gone before grace expires and cannot trigger the #757 re-kill).
# Counting DELIVERED frames — not wall-clock — is what makes detection
# independent of host load: a send loop starved under parallel-game CPU
# pressure still delivers exactly this many death frames, just spread over more
# wall-clock, instead of skipping the death window entirely.
DEATH_DELIVERY_FRAMES = 8


class MockControllerService(controller_manager_mock_pb2_grpc.MockControllerServiceServicer):
    """Service for controlling mock controllers during integration tests."""

    def __init__(self, backend: "MockAdapter"):
        """
        Initialize mock control service.

        Args:
            backend: MockAdapter instance to control
        """
        self.backend = backend
        self.auto_end_task: asyncio.Task | None = None  # Background task for auto-ending games
        logger.info("MockControllerService initialized")

    def SimulateMovement(self, request, _context):
        """Simulate controller movement by setting acceleration values."""
        try:
            serial = request.serial
            if serial not in self.backend.controllers:
                return controller_manager_mock_pb2.MovementResponse(
                    success=False, error=f"Controller {serial} not found"
                )

            # Update acceleration
            self.backend.controllers[serial][StateKey.ACCEL] = {
                AxisKey.X: request.accel_x,
                AxisKey.Y: request.accel_y,
                AxisKey.Z: request.accel_z,
            }

            logger.info(
                f"Mock: Set accel for {serial}: ({request.accel_x:.2f}, {request.accel_y:.2f}, {request.accel_z:.2f})"
            )
            return controller_manager_mock_pb2.MovementResponse(success=True, error="")

        except Exception as e:
            logger.error(f"SimulateMovement error: {e}")
            return controller_manager_mock_pb2.MovementResponse(success=False, error=str(e))

    def SimulateDeath(self, request, _context):
        """Simulate death by setting high acceleration and holding it briefly."""
        try:
            serial = request.serial
            if serial not in self.backend.controllers:
                return controller_manager_mock_pb2.DeathResponse(success=False, accel_magnitude=0.0)

            # Set death-level acceleration (matches mock_server.py)
            death_accel = {AxisKey.X: 5.0, AxisKey.Y: 3.0, AxisKey.Z: 4.0}
            accel_mag = (5.0**2 + 3.0**2 + 4.0**2) ** 0.5  # ~7.07g

            # Latch the death by a DELIVERED-frame budget, not wall-clock (#926):
            # the gameplay send path decrements it once per streamed frame, so
            # the death-level acceleration is guaranteed to reach the game in
            # DEATH_DELIVERY_FRAMES frames — enough to cross the EMA death
            # threshold — regardless of how starved the send loop is under
            # parallel-game CPU load. The wall-clock DEATH_HOLD_SECONDS now only
            # caps a latch that no gameplay stream is draining (controller not
            # in a game), so it still clears well below the 2.0s post-respawn
            # grace window and cannot cause the #757 re-kill.
            controller = self.backend.controllers[serial]
            controller["death_accel"] = death_accel
            controller["death_frames_remaining"] = DEATH_DELIVERY_FRAMES
            controller["death_hold_until"] = time.time() + DEATH_HOLD_SECONDS

            logger.info(
                f"Mock: Simulated death for {serial} with {accel_mag:.2f}g acceleration, "
                f"holding for {DEATH_DELIVERY_FRAMES} delivered frames "
                f"(safety cap {DEATH_HOLD_SECONDS}s)"
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

    async def SetAutoGameEnd(self, request, _context):
        """
        Enable/disable auto game end feature.

        When enabled, automatically sets high acceleration on all but one player
        after the specified duration.
        """
        try:
            # Cancel existing task if any
            if self.auto_end_task and not self.auto_end_task.done():
                self.auto_end_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.auto_end_task
                self.auto_end_task = None

            if request.enabled:
                # Start new background task
                self.auto_end_task = asyncio.create_task(self._auto_end_game(request.duration_seconds))
                logger.info(f"Mock: Auto game end enabled: will kill players after {request.duration_seconds}s")
                return controller_manager_mock_pb2.AutoGameEndResponse(success=True, error="")

            logger.info("Mock: Auto game end disabled")
            return controller_manager_mock_pb2.AutoGameEndResponse(success=True, error="")

        except Exception as e:
            logger.error(f"SetAutoGameEnd error: {e}")
            return controller_manager_mock_pb2.AutoGameEndResponse(success=False, error=str(e))

    async def _auto_end_game(self, duration: float):
        """Background task to auto-end game after duration."""
        try:
            logger.info(f"Mock: Waiting {duration}s before auto-ending game...")
            await asyncio.sleep(duration)

            # Kill all but one player (leave winner)
            serials = list(self.backend.controllers.keys())
            if len(serials) > 1:
                # Leave the last player alive (winner)
                players_to_kill = serials[:-1]
                logger.info(
                    f"Mock: Auto-ending game: killing {len(players_to_kill)} players, "
                    f"leaving {serials[-1]} alive as winner"
                )

                for serial in players_to_kill:
                    # Simulate death by directly setting controller state
                    controller = self.backend.controllers.get(serial)
                    if controller:
                        # Set death-level acceleration (same as SimulateDeath):
                        # delivered-frame latch + wall-clock safety cap (#926).
                        controller["death_accel"] = {AxisKey.X: 5.0, AxisKey.Y: 3.0, AxisKey.Z: 4.0}
                        controller["death_frames_remaining"] = DEATH_DELIVERY_FRAMES
                        controller["death_hold_until"] = time.time() + DEATH_HOLD_SECONDS
                        logger.info(f"Mock: Auto-killed player {serial}")
                    await asyncio.sleep(0.3)  # Stagger deaths for better trace visualization

            logger.info("Mock: Auto game end complete")

        except asyncio.CancelledError:
            logger.info("Mock: Auto game end cancelled")
            raise
        except Exception as e:
            logger.error(f"Mock: Error in auto game end: {e}", exc_info=True)

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
        """Remove a mock controller."""
        try:
            serial = request.serial
            removed = self.backend.remove_controller(serial)
            if removed:
                logger.info(f"Mock: Removed controller {serial}")
                return controller_manager_mock_pb2.RemoveControllerResponse(success=True)
            return controller_manager_mock_pb2.RemoveControllerResponse(
                success=False, error=f"Controller {serial} not found"
            )
        except Exception as e:
            logger.error(f"RemoveController error: {e}")
            return controller_manager_mock_pb2.RemoveControllerResponse(success=False, error=str(e))

    def AddControllers(self, request, _context):
        """Add multiple mock controllers at once.

        request.reserved/request.tag apply to every added controller.
        """
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
