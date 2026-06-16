"""
ControllerManager gRPC servicer implementation.

Contains the ControllerManagerServicer class that handles all gRPC methods
for managing PS Move controllers.
"""

import asyncio
import logging
import os

# Import protobuf
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import contextlib

from opentelemetry import trace

from lib.controller_constants import ControllerInfoKey, normalize_serial
from proto import controller_manager_pb2, controller_manager_pb2_grpc
from services.controller_manager import metrics

# Phase 57: Backend abstraction for platform independence
from services.controller_manager.backend_factory import create_backend
from services.controller_manager.button_detector import ButtonDetector
from services.controller_manager.discovery import PeriodicRescanTimer
from services.controller_manager.discovery_loop import DiscoveryLoop, visible_serials
from services.controller_manager.effects_base import ControllerEffectsBase
from services.controller_manager.event_publisher import EventPublisher as EventPublisherHelper
from services.controller_manager.feedback_manager import FeedbackManager
from services.controller_manager.monitoring import ControllerMonitoring
from services.controller_manager.name_manager import NameManager
from services.controller_manager.state_cache import StateCache

logger = logging.getLogger(__name__)


class ControllerManagerServicer(controller_manager_pb2_grpc.ControllerManagerServiceServicer, ControllerEffectsBase):
    """
    ControllerManager gRPC servicer.

    Manages PS Move controllers:
    - Discovery and pairing
    - Controller process spawning
    - State monitoring and streaming
    - Health checking

    Phase 40: Inherits from ControllerEffectsBase for shared effect logic.
    """

    def __init__(self):
        """Initialize controller manager."""
        ControllerEffectsBase.__init__(self)  # Initialize effects base class

        # Phase 57: Initialize backend (platform-agnostic)
        self.backend = create_backend()
        logger.info(f"Using controller backend: {self.backend.__class__.__name__}")

        self.tracked_controllers: dict[str, dict] = {}  # serial -> controller info
        self.controller_states: dict[str, dict] = {}  # serial -> state dict from backend
        self.paired_serials: list[str] = []
        self.controller_processes: dict[str, Any] = {}  # serial -> process (for cleanup)

        # Note: state_lock removed - no longer needed with async discovery loop
        # All operations run on the same event loop, so no cross-thread coordination required

        # Streaming subscribers (Phase 34: async queue and lock)
        self.stream_subscribers: dict[str, asyncio.Queue] = {}
        self.stream_lock = asyncio.Lock()

        # Button event streaming (Phase 41, Phase 34: async queue and lock)
        self.button_event_subscribers: dict[str, asyncio.Queue] = {}
        self.button_event_lock = asyncio.Lock()

        # Delta update tracking (Phase 26 - Part 3)
        # Store last sent state per subscriber per controller
        # Format: {subscriber_id: {serial: ControllerState}}
        self.last_sent_states: dict[str, dict[str, Any]] = {}

        # Event publisher for cross-thread communication (Phase refactor)
        self.event_publisher = EventPublisherHelper()

        # Battery monitoring - Phase 39, extracted to monitoring.py
        # NOTE: RSSI monitoring is handled by the host pairing-daemon
        # NOTE: Must be initialized before StateCache which depends on it
        self.monitoring = ControllerMonitoring(
            low_battery_threshold=1,
        )

        # State caching (Phase 18 - Task 1, refactored)
        self.state_cache_manager = StateCache(self.monitoring)
        self.state_cache_manager.set_controller_states(self.controller_states)

        # Button detector for button transitions (Phase 41, refactored)
        self.button_detector = ButtonDetector(self.event_publisher)
        self.button_detector.set_subscribers(self.button_event_subscribers)

        # Feedback manager for LED colors, vibration, and effects (Phase refactor)
        self.feedback_manager = FeedbackManager(
            backend=self.backend,
            tracked_controllers=self.tracked_controllers,
        )

        # Phase 79: Periodic rescan timer for externally paired controllers
        self.rescan_timer = PeriodicRescanTimer(interval=10.0)

        # Issue #7: Name manager for human-readable controller names
        self.name_manager = NameManager()

        # Discovery loop (extracted to discovery_loop.py)
        # Note: start() must be called from async context (done in first stream handler)
        self.discovery_loop = DiscoveryLoop(
            backend=self.backend,
            tracked_controllers=self.tracked_controllers,
            controller_states=self.controller_states,
            button_detector=self.button_detector,
            state_cache_manager=self.state_cache_manager,
            feedback_manager=self.feedback_manager,
            monitoring=self.monitoring,
            rescan_timer=self.rescan_timer,
            paired_serials=self.paired_serials,
            base_colors=self.feedback_manager.base_colors,
            event_publisher=self.event_publisher,
            name_manager=self.name_manager,
        )
        self._discovery_started = False

        logger.info("ControllerManager initialized")

    def _ensure_discovery_started(self) -> None:
        """Start discovery loop if not already started.

        Must be called from async context (event loop must be running).
        """
        if not self._discovery_started:
            self.discovery_loop.start()
            self._discovery_started = True

    async def _set_led_color(self, serial: str, color: tuple[int, int, int]):
        """Set LED color on a controller (async).

        Implements abstract method from ControllerEffectsBase.
        Delegates to feedback_manager for actual LED control.

        Args:
            serial: Controller serial number
            color: RGB tuple (0-255, 0-255, 0-255)
        """
        await self.feedback_manager._set_led_color(serial, color)

    async def _send_initial_connection_events(self, subscriber_id: str, event_queue: asyncio.Queue) -> None:
        """Send initial connection events for all currently tracked controllers.

        This allows new subscribers to immediately know about existing controllers.
        """
        # Snapshot to avoid RuntimeError if dict changes at await points
        tracked_snapshot = dict(self.tracked_controllers)
        # Reserved controllers (#777) are never announced to button-stream
        # consumers (the menu): skip their connect events and exclude them
        # from the connected_serials roster.
        all_serials = visible_serials(tracked_snapshot)
        for serial, info in tracked_snapshot.items():
            if info.get(ControllerInfoKey.RESERVED, False):
                continue
            battery = info.get(ControllerInfoKey.BATTERY, 0)
            name = info.get(ControllerInfoKey.NAME, "")
            connect_event = controller_manager_pb2.ButtonEvent(
                serial=serial,
                timestamp=int(time.time() * 1000),
                battery=battery,
                event_type=controller_manager_pb2.EVENT_CONNECT,
                name=name,
                connected_serials=all_serials,
            )
            try:
                event_queue.put_nowait(connect_event)
                logger.debug(f"[{subscriber_id}] Sent initial connection event for {serial} ({name})")
            except asyncio.QueueFull:
                logger.warning(f"[{subscriber_id}] Queue full, skipping initial event for {serial}")

        logger.info(f"[{subscriber_id}] Sent initial connection events for {len(all_serials)} controllers")

    async def _process_base_color_command(self, cmd, subscriber_id: str, stream_label: str) -> None:
        """Process a base_color command from either stream type.

        Delegates to feedback_manager.apply_base_color() for the cancel+apply logic.
        """
        serial = cmd.serial
        color = (cmd.color.r, cmd.color.g, cmd.color.b)

        if serial and serial in self.tracked_controllers:
            await self.feedback_manager.apply_base_color(serial, color, label=stream_label)
            logger.debug(f"[{subscriber_id}] Base color set: serial={serial}, rgb={color}")

        metrics.stream_commands_total.labels(command_type="base_color").inc()

    async def _process_game_effect_command(self, cmd, subscriber_id: str) -> None:
        """Process a game_effect command from either stream type."""
        effect_color = None
        if cmd.HasField("color"):
            effect_color = (cmd.color.r, cmd.color.g, cmd.color.b)
        await self.feedback_manager.handle_game_effect(
            cmd.serial,
            cmd.effect,
            subscriber_id,
            color=effect_color,
            duration_ms=cmd.duration_ms,
            speed=cmd.speed,
            trace_parent=cmd.trace_parent,
            trace_state=cmd.trace_state,
        )

        effect_name = controller_manager_pb2.GameEffect.Name(cmd.effect)
        logger.debug(f"[{subscriber_id}] Game effect: serial={cmd.serial or 'all'}, effect={effect_name}")

        metrics.stream_commands_total.labels(command_type="game_effect").inc()

    async def StreamButtonEvents(self, request_iterator, context):
        """
        Stream button press/release events as they occur (Phase 41).
        Phase XX: Made bidirectional for LED state ownership - menu can send base colors and effects.

        This is an event-driven stream - events are only sent when buttons
        change state (press or release), not on every frame.
        """
        # Ensure discovery loop is running (async task, needs event loop)
        self._ensure_discovery_started()

        subscriber_id = f"button_stream_{time.time()}"

        # Set main event loop reference for event publisher (used by button detector)
        if self.event_publisher.main_loop is None:
            self.event_publisher.set_main_loop(asyncio.get_running_loop())

        # Enrich the server span created by the gRPC interceptor
        span = trace.get_current_span()
        span.update_name("StreamButtonEvents")
        span.set_attribute("subscriber.id", subscriber_id)

        # Create queue for this subscriber (Phase 34: asyncio.Queue)
        # Increased from 100 to 500 to prevent event drops with many controllers
        event_queue = asyncio.Queue(maxsize=500)

        async with self.button_event_lock:  # Phase 34: async lock
            self.button_event_subscribers[subscriber_id] = event_queue

        # Update stream metrics (Phase 38)
        metrics.active_streams.inc()

        # Note: Don't clear base_colors here - effects may still be running and need
        # to restore to current base color. Menu will overwrite colors when it sends
        # new base_color commands for each controller.

        await self._send_initial_connection_events(subscriber_id, event_queue)

        # Background task to read client control messages
        async def read_client_controls():
            try:
                async for control_msg in request_iterator:
                    if control_msg.HasField("config"):
                        logger.info(f"[{subscriber_id}] Button stream configured")
                    elif control_msg.HasField("base_color"):
                        await self._process_base_color_command(control_msg.base_color, subscriber_id, "ButtonStream")
                    elif control_msg.HasField("game_effect"):
                        await self._process_game_effect_command(control_msg.game_effect, subscriber_id)
            except Exception as e:
                logger.error(f"[{subscriber_id}] Error reading client controls: {e}", exc_info=True)

        # Start background task to read client controls
        control_task = asyncio.create_task(read_client_controls())

        logger.info(f"New button event subscriber: {subscriber_id}")

        try:
            while not context.cancelled():
                try:
                    # Wait for button events (Phase 34: async wait with timeout)
                    # Check for events every 1s to stay responsive to cancellation
                    try:
                        event = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                        yield event
                        # Track stream update (Phase 38)
                        metrics.stream_updates_total.labels(stream_type="button_events").inc()
                    except TimeoutError:  # Phase 34: asyncio exception
                        # No events, continue loop to check cancellation
                        continue

                except Exception as e:
                    logger.error(f"Button event stream error for {subscriber_id}: {e}")
                    break

        finally:
            # Cleanup: Cancel background task
            control_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await control_task

            # Cleanup (Phase 34: async lock)
            async with self.button_event_lock:
                if subscriber_id in self.button_event_subscribers:
                    del self.button_event_subscribers[subscriber_id]

            # Update stream metrics (Phase 38)
            metrics.active_streams.dec()

            logger.info(f"Button event subscriber disconnected: {subscriber_id}")

    async def _process_gameplay_config(self, config, subscriber_id: str, span) -> tuple[int, set | None]:
        """Process initial gameplay stream configuration.

        Returns:
            Tuple of (update_frequency_hz, controller_filter).
            Filter is None for all controllers, or a set of serials.
        """
        current_hz = config.update_frequency_hz or 30
        current_filter = None

        if config.colors:
            current_filter = set()
            for color_config in config.colors:
                serial = color_config.serial
                if serial:
                    current_filter.add(serial)
                    color = (color_config.color.r, color_config.color.g, color_config.color.b)
                    self.feedback_manager.base_colors[serial] = color
                    if serial in self.tracked_controllers:
                        await self.feedback_manager.set_controller_color(serial, color)
            logger.info(f"[{subscriber_id}] Set base colors for {len(current_filter)} controllers")

        logger.info(
            f"[{subscriber_id}] Stream configured: {current_hz}Hz, "
            f"filter={len(current_filter) if current_filter else 'all'} controllers"
        )
        span.set_attribute("update_frequency_hz", current_hz)
        span.set_attribute("initial_filter_count", len(current_filter) if current_filter else 0)

        return current_hz, current_filter

    def _process_filter_update(self, filter_update, subscriber_id: str, span, current_filter: set | None) -> set | None:
        """Process a mid-stream filter update.

        Returns:
            The new filter (None for all controllers, or set of serials).
        """
        new_filter = set(filter_update.serials) if filter_update.serials else None

        if new_filter != current_filter:
            old_count = len(current_filter) if current_filter else 0
            new_count = len(new_filter) if new_filter else 0

            logger.info(f"[{subscriber_id}] Filter updated: {old_count} → {new_count} controllers")

            span.add_event(
                "filter_updated",
                {"previous_count": old_count, "new_count": new_count},
            )
            return new_filter

        return current_filter

    def _build_gameplay_update(self, current_filter: set | None) -> controller_manager_pb2.GameplayDataUpdate:
        """Build a GameplayDataUpdate message from current controller state.

        Args:
            current_filter: Set of serial numbers to include, or None for all.

        Returns:
            GameplayDataUpdate protobuf message.
        """
        gameplay_data = []
        # Mock death latch is drained per DELIVERED frame, not by wall-clock
        # (#926): hold a reference once (duck-typed — only MockAdapter has it,
        # real hardware backends do not need it) and call it for each streamed
        # controller below.
        consume_death = getattr(self.backend, "consume_death_frame", None)
        # No dict copy needed — _build_gameplay_update is synchronous (no await
        # points), so no dict mutation can happen during iteration.
        for serial, info in self.tracked_controllers.items():
            if current_filter is not None and serial not in current_filter:
                continue

            full_state = self.state_cache_manager.build_or_get_cached_state(serial, info)

            # Account this delivered frame against any pending mock death latch:
            # mock deaths are held until they have been STREAMED to the game
            # enough times to cross the EMA threshold, rather than for a fixed
            # wall-clock window a starved send loop could skip entirely (#926).
            if consume_death is not None:
                consume_death(serial)

            # Drain health counters accumulated since last frame
            drops, errors, led_fails = self.discovery_loop.drain_health_counters(serial)
            health = None
            if drops or errors or led_fails:
                health = controller_manager_pb2.ControllerHealth(
                    poll_drops=drops,
                    poll_errors=errors,
                    led_failures=led_fails,
                )

            gd = controller_manager_pb2.GameplayData(
                serial=full_state.serial,
                move_num=full_state.move_num,
                battery=full_state.battery,
                team=full_state.team,
                color=full_state.color,
                accel=full_state.accel,
                gyro=full_state.gyro,
                rssi=full_state.rssi,
                name=full_state.name,
                health=health,
            )
            gameplay_data.append(gd)

        # Drain disconnect events accumulated since last frame (#580)
        disconnect_events = [
            controller_manager_pb2.DisconnectEvent(serial=s) for s in self.discovery_loop.drain_disconnects()
        ]

        return controller_manager_pb2.GameplayDataUpdate(
            controllers=gameplay_data,
            timestamp=int(time.time() * 1000),
            disconnects=disconnect_events,
        )

    async def StreamGameplayData(self, request_iterator, context):
        """
        Stream gameplay data with dynamic filtering via bidirectional communication (Phase 45).

        Client can send filter updates at any time to adjust which controllers
        are being monitored without restarting the stream. Supports color commands,
        game effects, and other stream-based feedback.

        Args:
            request_iterator: AsyncIterator of GameplayStreamControl messages from client
            context: gRPC context

        Yields:
            GameplayDataUpdate messages with filtered controller data
        """
        # Ensure discovery loop is running (async task, needs event loop)
        self._ensure_discovery_started()

        subscriber_id = f"gameplay_stream_{time.time()}"

        # Enrich the server span created by the gRPC interceptor
        span = trace.get_current_span()
        span.update_name("StreamGameplayData")
        span.set_attribute("subscriber.id", subscriber_id)

        # Update stream metrics
        metrics.active_streams.inc()

        # Stream state (updated by client messages)
        current_hz = 30  # Default Hz
        current_filter = None  # None = all controllers

        # Background task to read client updates
        async def read_client_updates():
            nonlocal current_hz, current_filter

            try:
                async for control_msg in request_iterator:
                    if control_msg.HasField("config"):
                        current_hz, current_filter = await self._process_gameplay_config(
                            control_msg.config, subscriber_id, span
                        )
                    elif control_msg.HasField("filter_update"):
                        current_filter = self._process_filter_update(
                            control_msg.filter_update, subscriber_id, span, current_filter
                        )
                    elif control_msg.HasField("base_color"):
                        await self._process_base_color_command(control_msg.base_color, subscriber_id, "GameplayStream")
                    elif control_msg.HasField("game_effect"):
                        await self._process_game_effect_command(control_msg.game_effect, subscriber_id)
            except Exception as e:
                logger.error(f"[{subscriber_id}] Error reading client updates: {e}", exc_info=True)

        # Start background task to read client updates
        update_task = asyncio.create_task(read_client_updates())

        logger.info(f"New gameplay subscriber: {subscriber_id}")

        try:
            # Stream gameplay data with consistent frame timing
            # Uses monotonic clock to account for processing time, reducing jitter
            next_frame_time = time.monotonic()

            while not context.cancelled():
                try:
                    # Check for flagd frequency override
                    if _frequency_override is not None and _frequency_override != current_hz:
                        logger.info(f"[{subscriber_id}] Frequency override: {current_hz} -> {_frequency_override} Hz")
                        current_hz = _frequency_override

                    # Calculate interval from current Hz
                    interval = 1.0 / current_hz

                    update = self._build_gameplay_update(current_filter)
                    yield update

                    # Track stream update
                    if update.controllers:
                        metrics.stream_updates_total.labels(stream_type="gameplay_data").inc()
                        metrics.streamed_controllers.observe(len(update.controllers))

                    # Fixed frame timing: sleep until next scheduled frame time
                    # This accounts for processing time to maintain consistent frame rate
                    next_frame_time += interval
                    sleep_time = next_frame_time - time.monotonic()

                    # If we're behind schedule, reset timing (prevents spiral of catch-up)
                    if sleep_time < 0:
                        next_frame_time = time.monotonic() + interval
                        sleep_time = interval
                        metrics.stream_frame_overruns_total.inc()

                    await asyncio.sleep(sleep_time)

                except Exception as e:
                    logger.error(f"[{subscriber_id}] Gameplay stream error: {e}", exc_info=True)
                    break

        finally:
            # Cleanup: Cancel background task
            update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await update_task

            # Update stream metrics
            metrics.active_streams.dec()

            logger.info(f"Gameplay subscriber disconnected: {subscriber_id}")

    # NOTE: Internal feedback methods moved to feedback_manager.py
    # NOTE: State cache methods moved to state_cache.py
    # NOTE: Button detection methods moved to button_detector.py
    # NOTE: Event publishing methods moved to event_publisher.py

    async def RenameController(self, request, _context):
        """Rename a controller with a custom human-readable name (Issue #7)."""
        span = trace.get_current_span()
        span.set_attribute("serial", request.serial)
        span.set_attribute("name", request.name)

        try:
            if not request.serial:
                return controller_manager_pb2.RenameControllerResponse(success=False, error="Serial number is required")
            if not request.name:
                return controller_manager_pb2.RenameControllerResponse(success=False, error="Name is required")

            serial = normalize_serial(request.serial)

            # Update the name
            self.name_manager.set_name(serial, request.name)

            # Update tracked_controllers if the controller is currently connected
            if serial in self.tracked_controllers:
                self.tracked_controllers[serial][ControllerInfoKey.NAME] = request.name

            logger.info(f"Renamed controller {serial} to '{request.name}'")
            return controller_manager_pb2.RenameControllerResponse(success=True, error="")

        except Exception as e:
            span.record_exception(e)
            logger.error(f"RenameController error: {e}", exc_info=True)
            return controller_manager_pb2.RenameControllerResponse(success=False, error=str(e))

    async def GetConnectedControllers(self, _request, _context):
        """Return the authoritative live roster of connected controllers (#1153).

        Unlike the event-piggybacked ``connected_serials`` (only refreshed on the
        next CONNECT/DISCONNECT), this reads the current ``tracked_controllers``
        synchronously so a client can reconcile ghost controllers after a missed
        disconnect with no later event. Reserved controllers (#777) are excluded,
        mirroring the roster sent on the button-event stream.
        """
        span = trace.get_current_span()

        # Snapshot to stay consistent if the discovery loop mutates the dict.
        # This method has no await points, but a snapshot keeps it robust.
        tracked_snapshot = dict(self.tracked_controllers)

        # visible_serials() is the canonical "what button-stream consumers (the
        # menu) may see" filter (excludes reserved controllers, #777). Reuse it
        # so the RPC and the stream roster never silently diverge.
        serials = visible_serials(tracked_snapshot)
        controllers = [
            controller_manager_pb2.ConnectedController(
                serial=serial,
                name=tracked_snapshot[serial].get(ControllerInfoKey.NAME, ""),
                battery=tracked_snapshot[serial].get(ControllerInfoKey.BATTERY, 0),
            )
            for serial in serials
        ]

        span.set_attribute("controller.count", len(serials))
        logger.debug(f"GetConnectedControllers: {len(serials)} connected")

        return controller_manager_pb2.GetConnectedControllersResponse(
            connected_serials=serials,
            controllers=controllers,
        )

    async def shutdown(self):
        """Shutdown the controller manager."""
        logger.info("Shutting down ControllerManager...")

        # Stop discovery loop
        self.discovery_loop.stop()

        # Stop all controller processes
        for _serial, proc in self.controller_processes.items():
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=2.0)

        await self.discovery_loop.wait_stopped(timeout_seconds=5.0)


# ---------------------------------------------------------------------------
# Flagd frequency override — simple listener using lib/feature_flags
# ---------------------------------------------------------------------------

_frequency_override: int | None = None
_frequency_listener_initialized = False


def init_frequency_listener() -> None:
    """Register event handler for game_loop.update_frequency_hz flag changes."""
    global _frequency_listener_initialized
    if _frequency_listener_initialized:
        return

    from openfeature.provider import ProviderEvent

    from lib.feature_flags import get_flag_client

    client = get_flag_client("system")
    client.add_handler(ProviderEvent.PROVIDER_CONFIGURATION_CHANGED, _on_system_flags_changed)
    _frequency_listener_initialized = True

    # Initial read
    _on_system_flags_changed(None)
    logger.info("Frequency flag listener registered on system client")


def _on_system_flags_changed(event_details) -> None:
    """Update frequency override when flagd config changes."""
    # Skip if event specifies changed flags and ours isn't among them
    changed = getattr(event_details, "flags_changed", None)
    if changed is not None and "game_loop.update_frequency_hz" not in changed:
        return

    global _frequency_override
    try:
        from openfeature.evaluation_context import EvaluationContext

        from lib.feature_flags import get_flag_client

        client = get_flag_client("system")
        hz = client.get_integer_value("game_loop.update_frequency_hz", 0, EvaluationContext())
        if hz <= 0:
            return
        new_hz = max(1, min(100, hz))
        if new_hz != _frequency_override:
            logger.info(f"Streaming frequency override from flagd: {_frequency_override} -> {new_hz} Hz")
            _frequency_override = new_hz
            metrics.stream_frequency_changes_total.inc()
            metrics.stream_current_frequency_hz.set(new_hz)
    except Exception as e:
        logger.warning(f"Failed to read frequency flag: {e}")
