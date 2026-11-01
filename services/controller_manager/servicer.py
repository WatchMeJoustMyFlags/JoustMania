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

import grpc
from opentelemetry import trace

from lib.colors import Colors
from lib.controller_constants import ControllerInfoKey, normalize_serial
from lib.telemetry import get_tracer
from proto import controller_manager_pb2, controller_manager_pb2_grpc
from services.controller_manager import metrics

# Phase 57: Backend abstraction for platform independence
from services.controller_manager.backend_factory import create_backend
from services.controller_manager.button_detector import ButtonDetector
from services.controller_manager.discovery import PeriodicRescanTimer
from services.controller_manager.discovery_loop import DiscoveryLoop, visible_serials
from services.controller_manager.effects_base import ControllerEffectsBase
from services.controller_manager.event_publisher import EventPublisher as EventPublisherHelper
from services.controller_manager.feedback_manager import FeedbackManager, clamp_identify_duration
from services.controller_manager.monitoring import ControllerMonitoring
from services.controller_manager.name_manager import NameManager
from services.controller_manager.state_builder import StateBuilder

# LED of a game's player that reconnects while the game runs: it is out (UC-002 BR-008).
OUT_OF_GAME_COLOR = Colors.Black.value

logger = logging.getLogger(__name__)

# A closed game stream's shield layers end after this delay unless a new game stream
# claims the controller first (the coordinator reconnects and re-sends, #1837).
STREAM_CLOSE_SHIELD_CLEANUP_DELAY_S = 7.0

# A closing gameplay stream applies the commands it already received for at most this long (#2540).
STREAM_CLOSE_DRAIN_TIMEOUT_S = 1.0
# After the drain, the applying task gets this long to stop on its cancel before the close goes on.
STREAM_CLOSE_CANCEL_GRACE_S = 0.1
# Ends a gameplay stream's command queue: everything queued before it is applied first.
_END_OF_CONTROLS = object()

tracer = get_tracer(__name__)


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
        self._backend_closed = False
        logger.info(f"Using controller backend: {self.backend.__class__.__name__}")

        self.tracked_controllers: dict[str, dict] = {}  # serial -> controller info
        self.controller_states: dict[str, dict] = {}  # serial -> state dict from backend
        # Labelled controller_input_lag_seconds children, cached per serial so the
        # per-frame path skips the labels() lookup (same idea as _accel_gauges, #542).
        self._input_lag_histograms: dict[str, Any] = {}
        self.paired_serials: list[str] = []

        # Note: state_lock removed - no longer needed with async discovery loop
        # All operations run on the same event loop, so no cross-thread coordination required

        # Streaming subscribers (Phase 34: async queue and lock)
        self.stream_subscribers: dict[str, asyncio.Queue] = {}
        self.stream_lock = asyncio.Lock()

        # Button event streaming (Phase 41, Phase 34: async queue and lock)
        self.button_event_subscribers: dict[str, asyncio.Queue] = {}
        self.button_event_lock = asyncio.Lock()

        # Players of each open game gameplay stream (a stream configured with
        # player colours), by subscriber id: Identify is lobby-only (UC-004 BR-003).
        self._game_stream_serials: dict[str, set[str]] = {}
        # Per open game stream: the players its game still lists alive (its last filter update,
        # the whole roster until the first one) and the serials whose disconnect it was handed.
        # A re-registering player is out only when both say so (UC-002 BR-008, #2466).
        self._game_stream_alive: dict[str, set[str]] = {}
        self._handed_disconnects: dict[str, set[str]] = {}
        # Disconnects not yet delivered, per open game stream. A stream only gets
        # disconnects that happen while it is open (UC-002 BR-004, #1607).
        self._pending_disconnects: dict[str, list[str]] = {}
        # Serials reported as disconnected; pruned once they are tracked again.
        self._dropped_serials: set[str] = set()
        # Delayed shield cleanups of closed game streams (#1837); kept so shutdown can cancel them.
        self._shield_cleanup_tasks: set[asyncio.Task] = set()

        # Delta update tracking (Phase 26 - Part 3)
        # Store last sent state per subscriber per controller
        # Format: {subscriber_id: {serial: ControllerState}}
        self.last_sent_states: dict[str, dict[str, Any]] = {}

        # Event publisher for cross-thread communication (Phase refactor)
        self.event_publisher = EventPublisherHelper()

        # Battery monitoring - Phase 39, extracted to monitoring.py
        # NOTE: RSSI monitoring is handled by the host pairing-daemon
        # NOTE: Must be initialized before StateBuilder which depends on it
        self.monitoring = ControllerMonitoring(
            low_battery_threshold=1,
        )

        # State caching (Phase 18 - Task 1, refactored)
        self.state_builder = StateBuilder(self.monitoring)
        self.state_builder.set_controller_states(self.controller_states)

        # Button detector for button transitions (Phase 41, refactored)
        self.button_detector = ButtonDetector(self.event_publisher)
        self.button_detector.set_subscribers(self.button_event_subscribers)

        # Feedback manager for LED colors, vibration, and effects (Phase refactor)
        self.feedback_manager = FeedbackManager(
            backend=self.backend,
            tracked_controllers=self.tracked_controllers,
        )
        # Stream the live LED colour (effect frames included) to clients (#1301).
        self.state_builder.set_led_colors(self.feedback_manager.current_colors)

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
            feedback_manager=self.feedback_manager,
            monitoring=self.monitoring,
            rescan_timer=self.rescan_timer,
            paired_serials=self.paired_serials,
            base_colors=self.feedback_manager.base_colors,
            event_publisher=self.event_publisher,
            name_manager=self.name_manager,
            reconnect_color=self._reconnect_color,
        )
        self._discovery_started = False
        # serial -> (GapStats, ConnectionQuality) last sent on dashboard frames (UC-035 BR-005)
        self._quality_pb_cache: dict[str, tuple[object, controller_manager_pb2.ConnectionQuality]] = {}

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
            shield_hold=cmd.shield_hold if cmd.HasField("shield_hold") else None,
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
            # A game claims these controllers: end any running identify first so
            # it cannot paint over the game's colours or countdown (UC-004 BR-003).
            for color_config in config.colors:
                if color_config.serial:
                    await self.feedback_manager.cancel_identify(color_config.serial)
            for color_config in config.colors:
                serial = color_config.serial
                if serial:
                    current_filter.add(serial)
                    color = (color_config.color.r, color_config.color.g, color_config.color.b)
                    self.feedback_manager.base_colors[serial] = color
                    if serial in self.tracked_controllers:
                        await self.feedback_manager.set_controller_color(serial, color)
            logger.info(f"[{subscriber_id}] Set base colors for {len(current_filter)} controllers")
            # The game's whole roster, dead players included (UC-004 BR-003).
            self._open_game_stream(subscriber_id, current_filter)

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
        if subscriber_id in self._game_stream_serials:
            # A game stream's filter is its alive set; an empty one arrives as None (#1595).
            self._game_stream_alive[subscriber_id] = new_filter or set()

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

    def _build_gameplay_update(
        self, current_filter: set | None, subscriber_id: str = ""
    ) -> controller_manager_pb2.GameplayDataUpdate:
        """Build a GameplayDataUpdate message from current controller state.

        UC-030 BR-002: each frame carries the latest sample per controller, not
        the maximum since the previous frame.

        Args:
            current_filter: Set of serial numbers to include, or None for all.
            subscriber_id: The stream building this frame; a pattern controller's
                frame index advances only on the stream that owns it (UC-046 BR-001).

        Returns:
            GameplayDataUpdate protobuf message.
        """
        gameplay_data = []
        # Mock death latch is drained per DELIVERED frame, not by wall-clock
        # (#926): only MockAdapter does anything, every other backend inherits
        # the no-op, so it is called for each streamed controller below.
        consume_death = self.backend.consume_death_frame
        # Trigger latch (#1837): a press since the previous delivered frame, drained per
        # game frame like the death latch so observer streams never steal it.
        consume_trigger = self.backend.consume_trigger_press
        # Motion patterns (UC-046, #1258): a pattern controller's DELIVERED frame k
        # carries f(k / r), evaluated here (once per delivered frame) rather than in
        # the 100 Hz poll, so game time follows delivered frames, not polls.
        pattern_frame = self.backend.pattern_frame
        pattern_serials = self.backend.pattern_serials()
        now = time.time()
        # Input lag is a game-path signal: only game streams (which always send a
        # controller filter) record it, not the dashboard's unfiltered stream, so
        # menu/dashboard traffic never skews the latency panels.
        record_input_lag = current_filter is not None
        # Game signals are read once: disconnects, health counters and the mock
        # death latch are drained only by game streams (those that sent colours).
        # An observer stream (one that never sent colours, e.g. the dashboard's)
        # never consumes them, or it would steal them from the game. A game stream
        # keeps draining while its alive filter is empty (UC-035 BR-006, #1595).
        game_stream = self._is_game_stream(subscriber_id)
        # An empty alive filter arrives here as None (every controller), so the
        # stream consumes only its own roster, never another game's mocks (#1610).
        roster = self._game_stream_serials.get(subscriber_id, set())
        # No dict copy needed — _build_gameplay_update is synchronous (no await
        # points), so no dict mutation can happen during iteration.
        for serial, info in self.tracked_controllers.items():
            if current_filter is not None and serial not in current_filter:
                continue

            # Plain values straight from the state dict (#2353): one GameplayData per
            # controller, no ControllerState or Vector3 copies in between.
            inputs = self.state_builder.frame_inputs(serial, info)
            accel = inputs.accel
            pattern_trigger = False
            if serial in pattern_serials:
                # Evaluated BEFORE consume_death so an active latch is delivered first (BR-012).
                explicit = current_filter is not None and serial in current_filter
                pattern_out = pattern_frame(serial, subscriber_id, explicit)
                if pattern_out is not None:
                    pattern_accel, pattern_trigger = pattern_out
                    accel = {"x": pattern_accel["x"], "y": pattern_accel["y"], "z": pattern_accel["z"]}

            # Account this delivered frame against any pending mock death latch:
            # mock deaths are held until they have been STREAMED to the game
            # enough times to cross the EMA threshold, rather than for a fixed
            # wall-clock window a starved send loop could skip entirely (#926).
            if game_stream and serial in roster:
                consume_death(serial)

            # The trigger is the level OR a press latched since the last game frame.
            # A pattern controller presses by its trigger policy (UC-046 BR-018), owner stream only.
            trigger_pressed = inputs.trigger_pressed or pattern_trigger
            if game_stream and serial in roster and consume_trigger(serial) is True:
                trigger_pressed = True

            # Drain health counters accumulated since last frame (game streams only)
            health = None
            drops, errors, led_fails = (
                self.discovery_loop.drain_health_counters(serial) if game_stream and serial in roster else (0, 0, 0)
            )
            if drops or errors or led_fails:
                health = controller_manager_pb2.ControllerHealth(
                    poll_drops=drops,
                    poll_errors=errors,
                    led_failures=led_fails,
                )

            gd = controller_manager_pb2.GameplayData(
                serial=inputs.serial,
                move_num=inputs.move_num,
                battery=inputs.battery,
                team=inputs.team,
                color=inputs.color,
                accel=accel,
                gyro=inputs.gyro,
                rssi=inputs.rssi,
                name=inputs.name,
                health=health,
                connection_quality=self._connection_quality_msg(serial) if current_filter is None else None,
                trigger_pressed=trigger_pressed,
            )
            gameplay_data.append(gd)

            # Pattern controllers synthesise accel per delivered frame, so the
            # polled sample's age says nothing about what they carry.
            if record_input_lag and serial not in pattern_serials:
                self._record_input_lag(serial, now)

        # Drain disconnect events accumulated since last frame (#580; game streams only)
        disconnect_events = (
            [controller_manager_pb2.DisconnectEvent(serial=s) for s in self._take_disconnects(subscriber_id)]
            if game_stream
            else []
        )

        return controller_manager_pb2.GameplayDataUpdate(
            controllers=gameplay_data,
            timestamp=int(time.time() * 1000),
            disconnects=disconnect_events,
        )

    def _connection_quality_msg(self, serial: str) -> controller_manager_pb2.ConnectionQuality | None:
        """Connection quality for the dashboard's unfiltered stream (UC-035 BR-005).

        Game (filtered) streams do not carry it: the game loop has no use for it.
        """
        stats = self.discovery_loop.connection_quality(serial)
        if stats is None:
            return None
        # The stats change at the ~1 Hz health emission; build the message once per change.
        cached = self._quality_pb_cache.get(serial)
        if cached is not None and cached[0] is stats:
            return cached[1]
        msg = controller_manager_pb2.ConnectionQuality(
            level=stats.quality,
            report_gap_p95_ms=stats.p95_ms,
            report_gap_max_ms=stats.max_ms,
        )
        self._quality_pb_cache[serial] = (stats, msg)
        return msg

    def _record_input_lag(self, serial: str, now: float) -> None:
        """Observe how old the sample in this gameplay frame is (FR-083).

        Measures CM poll pick-up -> gameplay stream send. The Bluetooth/HID hop
        before the poll is not included: SensorData has no hardware timestamp.
        """
        age = self.discovery_loop.sample_age_seconds(serial, now)
        if age is None:
            return
        histogram = self._input_lag_histograms.get(serial)
        if histogram is None:
            histogram = metrics.controller_input_lag_seconds.labels(serial=serial)
            self._input_lag_histograms[serial] = histogram
        histogram.observe(age)

    def _pattern_stream_rate(self, current_filter: set | None) -> int | None:
        """Pinned rate when the stream's explicit filter names a pattern controller."""
        if not current_filter:
            return None
        return self.backend.pattern_stream_rate(current_filter)

    async def _process_gameplay_control(
        self, control_msg, subscriber_id: str, span, current_filter: set | None
    ) -> tuple[int | None, set | None]:
        """Apply one GameplayStreamControl message.

        Returns (the config's rate or None when the message is not a config, the stream's filter
        afterwards). Only a config or a filter update changes the filter.
        """
        if control_msg.HasField("config"):
            return await self._process_gameplay_config(control_msg.config, subscriber_id, span)
        if control_msg.HasField("filter_update"):
            return None, self._process_filter_update(control_msg.filter_update, subscriber_id, span, current_filter)
        if control_msg.HasField("base_color"):
            await self._process_base_color_command(control_msg.base_color, subscriber_id, "GameplayStream")
        elif control_msg.HasField("game_effect"):
            await self._process_game_effect_command(control_msg.game_effect, subscriber_id)
        return None, current_filter

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
        requested_hz = current_hz  # the client's configured rate
        current_filter = None  # None = all controllers

        # Commands read off the call, applied in arrival order by apply_client_updates (#2540).
        # Reading eagerly takes every command off the call as it arrives, so a command already
        # received is still applied when the client cancels the call (the game's stream close).
        pending_controls: asyncio.Queue = asyncio.Queue()

        async def apply_client_updates():
            nonlocal current_hz, requested_hz, current_filter

            try:
                while (control_msg := await pending_controls.get()) is not _END_OF_CONTROLS:
                    config_hz, current_filter = await self._process_gameplay_control(
                        control_msg, subscriber_id, span, current_filter
                    )
                    if config_hz is not None:
                        requested_hz = current_hz = config_hz
            except Exception as e:
                logger.error(f"[{subscriber_id}] Error applying client updates: {e}", exc_info=True)

        # Start background tasks to read and apply client updates
        read_task = asyncio.create_task(self._read_gameplay_controls(request_iterator, pending_controls, subscriber_id))
        apply_task = asyncio.create_task(apply_client_updates())

        logger.info(f"New gameplay subscriber: {subscriber_id}")

        try:
            # Stream gameplay data with consistent frame timing
            # Uses monotonic clock to account for processing time, reducing jitter
            next_frame_time = time.monotonic()

            while not context.cancelled():
                try:
                    # UC-046 FR-103: a stream naming pattern controllers runs at the
                    # archetype's pinned stream_rate_hz and ignores the flag override.
                    pattern_hz = self._pattern_stream_rate(current_filter)
                    if pattern_hz is not None:
                        if pattern_hz != current_hz:
                            logger.info(
                                f"[{subscriber_id}] Pattern stream rate pinned: {current_hz} -> {pattern_hz} Hz"
                            )
                            current_hz = pattern_hz
                    # Check for flagd frequency override
                    else:
                        target_hz = _stream_rate_hz(
                            requested_hz, self._is_game_stream(subscriber_id), _frequency_override
                        )
                        if target_hz != current_hz:
                            logger.info(f"[{subscriber_id}] Frequency override: {current_hz} -> {target_hz} Hz")
                            current_hz = target_hz

                    # Calculate interval from current Hz
                    interval = 1.0 / current_hz

                    update = self._build_gameplay_update(current_filter, subscriber_id)
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
                        # UC-046 A10: the pattern signal stays frame-indexed, but the
                        # game is no longer exact; count it per pattern controller.
                        if pattern_hz is not None:
                            self.backend.record_pattern_overrun(subscriber_id)

                    await asyncio.sleep(sleep_time)

                except Exception as e:
                    logger.error(f"[{subscriber_id}] Gameplay stream error: {e}", exc_info=True)
                    span.record_exception(e)
                    span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
                    # A non-OK status tells the coordinator the stream failed (UC-030 BR-006, #1930);
                    # an OK end would read as a normal end of the game.
                    await context.abort(grpc.StatusCode.INTERNAL, f"gameplay stream frame error: {e}")

        finally:
            # Cleanup: stop reading, then apply the commands already received (an end-of-game
            # WINNER_RAINBOW or SHIELD 0 included) before the roster is released (#2540).
            try:
                read_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await read_task
                await self._drain_gameplay_controls(apply_task, pending_controls, subscriber_id)
            finally:
                # Runs even when a second cancel interrupts the drain; that cancel goes on afterwards.
                self._release_closed_gameplay_stream(subscriber_id)

    def _release_closed_gameplay_stream(self, subscriber_id: str) -> None:
        """Release a closed gameplay stream's roster, shield layers, pattern controllers and stream gauge."""
        # Snapshot BEFORE the release: it forgets the roster (#1837).
        closed_roster = set(self._game_stream_serials.get(subscriber_id, ()))
        self._release_game_stream(subscriber_id)
        self._schedule_shield_cleanup(closed_roster)

        # A reconnecting game stream may claim this stream's pattern controllers.
        self.backend.release_pattern_stream(subscriber_id)

        # Update stream metrics
        metrics.active_streams.dec()

        logger.info(f"Gameplay subscriber disconnected: {subscriber_id}")

    @staticmethod
    async def _read_gameplay_controls(request_iterator, pending: asyncio.Queue, subscriber_id: str) -> None:
        """Take every control message off the call as it arrives, so a close cannot drop a received one (#2540)."""
        try:
            async for control_msg in request_iterator:
                pending.put_nowait(control_msg)
        except Exception as e:
            logger.error(f"[{subscriber_id}] Error reading client updates: {e}", exc_info=True)

    @staticmethod
    async def _drain_gameplay_controls(apply_task: asyncio.Task, pending: asyncio.Queue, subscriber_id: str) -> None:
        """Let a closing gameplay stream apply the commands it already received, bounded (#2540).

        No handler waits on an effect hold, so the drain is short; the bound only protects the
        close from a hung backend write. A command not applied by then is dropped with a warning.
        """
        pending.put_nowait(_END_OF_CONTROLS)
        try:
            await asyncio.wait_for(asyncio.shield(apply_task), timeout=STREAM_CLOSE_DRAIN_TIMEOUT_S)
        except TimeoutError:
            logger.warning(
                f"[{subscriber_id}] Stream close: {pending.qsize()} received commands not applied within "
                f"{STREAM_CLOSE_DRAIN_TIMEOUT_S} s"
            )
        except asyncio.CancelledError:
            pass  # the close itself was cancelled again: stop draining, the roster release still runs
        finally:
            apply_task.cancel()
            # Bounded too: a command that swallows the cancel must not hold the roster release (#2540).
            _done, still_running = await asyncio.wait({apply_task}, timeout=STREAM_CLOSE_CANCEL_GRACE_S)
            if still_running:
                logger.warning(f"[{subscriber_id}] Stream close: a command did not stop within the cancel grace")

    # NOTE: Internal feedback methods moved to feedback_manager.py
    # NOTE: Per-controller frame inputs are read in state_builder.py
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
                old_name = self.tracked_controllers[serial].get(ControllerInfoKey.NAME, "")
                self.tracked_controllers[serial][ControllerInfoKey.NAME] = request.name
                # #1295: swap the controller_info series so only the current
                # {serial,name} label set is exported (the agent reads it for player.name).
                metrics.remove_controller_info(serial, old_name)
                metrics.set_controller_info(serial, request.name)

            logger.info(f"Renamed controller {serial} to '{request.name}'")
            return controller_manager_pb2.RenameControllerResponse(success=True, error="")

        except Exception as e:
            span.record_exception(e)
            logger.error(f"RenameController error: {e}", exc_info=True)
            return controller_manager_pb2.RenameControllerResponse(success=False, error=str(e))

    def _open_game_stream(self, subscriber_id: str, serials: set[str]) -> None:
        """Register a game stream with only its own disconnects pending (UC-002 BR-004).

        Disconnects queued before it opened belong to the streams open at the time
        (or to none), so they are handed out first. The new stream starts with the
        roster controllers that dropped and are not connected again: a player who
        dropped before the stream opened (instructions clip, stream reconnect) is
        still reported, one that dropped and reconnected before the game is not
        (#1607), and one not discovered yet (a freshly added mock) is not either.
        A re-sent config keeps the stream's pending list.
        """
        self._collect_disconnects()
        self._dropped_serials -= self.tracked_controllers.keys()
        self._game_stream_serials[subscriber_id] = set(serials)
        self._game_stream_alive.setdefault(subscriber_id, set(serials))
        self._handed_disconnects.setdefault(subscriber_id, set())
        gone = sorted(s for s in serials if s in self._dropped_serials)
        self._pending_disconnects.setdefault(subscriber_id, gone)

    def _collect_disconnects(self) -> None:
        """Move queued disconnects to every open game stream; with none open they are dropped."""
        drained = self.discovery_loop.drain_disconnects()
        if not drained:
            return
        self._dropped_serials.update(drained)
        for pending in self._pending_disconnects.values():
            pending.extend(drained)

    def _take_disconnects(self, subscriber_id: str) -> list[str]:
        """Disconnects since this game stream's last frame (UC-002 BR-004)."""
        self._collect_disconnects()
        pending = self._pending_disconnects.get(subscriber_id)
        if not pending:
            return []
        self._pending_disconnects[subscriber_id] = []
        handed = self._handed_disconnects.get(subscriber_id)
        if handed is not None:
            handed.update(pending)
        return pending

    def _release_game_stream(self, subscriber_id: str) -> None:
        """Forget a closed game stream's players (UC-004 BR-003).

        A player that is not connected keeps no game colour: it would be its LED when it
        reconnects in the lobby, which owns it then (UC-002 BR-008, #2466).
        """
        roster = self._game_stream_serials.pop(subscriber_id, None) or set()
        self._pending_disconnects.pop(subscriber_id, None)
        self._game_stream_alive.pop(subscriber_id, None)
        self._handed_disconnects.pop(subscriber_id, None)
        for serial in roster:
            if serial not in self.tracked_controllers and not self._in_game(serial):
                self.feedback_manager.base_colors.pop(serial, None)
                # Nor an output the game wrote while it was away (#2471).
                self.backend.clear_led_output(serial)

    def _reconnect_color(self, serial: str) -> tuple[int, int, int] | None:
        """The LED colour of a controller that (re)registers, from its current owner (UC-002 BR-008, #2466).

        A player whose disconnect an open game stream was handed and that the game no longer
        lists alive was eliminated by it (UC-020 A2): it gets the out state (off) once, never
        its cached game colour. The off is not stored in ``base_colors``, so the game's own
        effects stay free to repaint it. A roster player first seen after the stream opened,
        or one whose disconnect the game dropped because it came back during the intro
        (BR-004), is alive and gets its game colour. Any other controller belongs to the
        lobby: its last lobby colour, if one was set (the menu repaints it on the connect
        event, UC-002 step 6).
        """
        if any(
            serial in self._handed_disconnects.get(sid, ()) and serial not in self._game_stream_alive.get(sid, roster)
            for sid, roster in self._game_stream_serials.items()
        ):
            return OUT_OF_GAME_COLOR
        return self.feedback_manager.base_colors.get(serial)

    def _schedule_shield_cleanup(self, serials: set[str]) -> None:
        """End the shield and charge layers of a closed game stream's players after a short delay.

        A coordinator crash must not leave a breathing controller in the lobby (#1837), but a
        coordinator reconnect re-claims the controllers within the delay and keeps its layers.
        """
        if not serials:
            return
        task = asyncio.create_task(self._shield_cleanup_after_delay(frozenset(serials)))
        self._shield_cleanup_tasks.add(task)
        task.add_done_callback(self._shield_cleanup_tasks.discard)

    async def _shield_cleanup_after_delay(self, serials: frozenset[str]) -> None:
        await asyncio.sleep(STREAM_CLOSE_SHIELD_CLEANUP_DELAY_S)
        for serial in serials:
            if self._in_game(serial):
                continue  # a new game stream claimed it: the layers are its to end
            try:
                await self.feedback_manager.end_shield_effect(serial, restore=True)
            except Exception as e:
                logger.warning(f"Shield cleanup after stream close failed for {serial}: {e}")

    def _is_game_stream(self, subscriber_id: str) -> bool:
        """A gameplay stream that sent colours is a game stream; any other is an observer (UC-035 BR-006)."""
        return subscriber_id in self._game_stream_serials

    def _in_game(self, serial: str) -> bool:
        """Whether the controller is a player of an open game gameplay stream."""
        return any(serial in serials for serials in self._game_stream_serials.values())

    def _identify_refusal(self, serial: str) -> tuple[str, str] | None:
        """(outcome, error) when Identify must be refused, else None (UC-004 A1-A3, A6)."""
        if not serial:
            return "invalid", "Serial number is required"
        info = self.tracked_controllers.get(serial)
        if info is None:
            return "not_connected", f"Controller {serial} is not connected"
        if info.get(ControllerInfoKey.RESERVED, False):
            return "reserved", f"Controller {serial} is reserved for a shadow game"
        if self._in_game(serial):
            return "in_game", f"Controller {serial} is in a running game"
        return None

    async def IdentifyController(self, request, _context):
        """Flash a controller white and rumble it so the operator can find it (UC-004, FR-121)."""
        serial = normalize_serial(request.serial) if request.serial else ""
        duration_ms = clamp_identify_duration(request.duration_ms)

        with tracer.start_as_current_span("controller.identify") as span:
            span.set_attribute("controller.serial", serial)
            span.set_attribute("effect.duration_ms", duration_ms)

            refusal = self._identify_refusal(serial)
            if refusal is None:
                started = await self.feedback_manager.start_identify(serial, duration_ms)
                if self._in_game(serial):
                    # A game claimed the controller while the identify was starting (UC-004 BR-003).
                    await self.feedback_manager.cancel_identify(serial)
                    refusal = ("in_game", f"Controller {serial} is in a running game")
                elif not started:
                    # start_identify otherwise only fails if the controller vanished meanwhile.
                    refusal = ("not_connected", f"Controller {serial} is not connected")

            outcome, error = refusal if refusal else ("started", "")
            span.set_attribute("identify.outcome", outcome)
            metrics.controller_identify_requests_total.labels(outcome=outcome).inc()
            logger.info(f"IdentifyController {serial or '<empty>'}: {outcome}")

            return controller_manager_pb2.IdentifyControllerResponse(
                success=outcome == "started", outcome=outcome, error=error, duration_ms=duration_ms
            )

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

        for cleanup in list(self._shield_cleanup_tasks):
            cleanup.cancel()

        # Stop discovery loop
        self.discovery_loop.stop()

        await self.discovery_loop.wait_stopped(timeout_seconds=5.0)

    async def close_backend(self):
        """Release the HID backend (handles, routes, info series). Idempotent.

        Call after the gRPC server stopped: streams still use the backend until
        then (#1726).
        """
        if self._backend_closed:
            return
        self._backend_closed = True
        await self.backend.shutdown()


# ---------------------------------------------------------------------------
# Flagd frequency override — simple listener using lib/feature_flags
# ---------------------------------------------------------------------------

_frequency_override: int | None = None


def _stream_rate_hz(requested_hz: int, game_stream: bool, override_hz: int | None) -> int:
    """Frame rate of a gameplay stream that names no pattern controller.

    A game stream (one that sent colours) runs at the ``game_loop.update_frequency_hz``
    flag when it resolves. An observer stream (one that never sent colours) runs at its requested rate,
    capped at the flag rate, so a dashboard tab asking for 30 Hz costs 30 Hz
    (UC-035 BR-006, #1595). Without the flag every stream runs at its request.
    """
    if override_hz is None:
        return requested_hz
    if not game_stream:
        return max(1, min(requested_hz, override_hz))
    return override_hz


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
