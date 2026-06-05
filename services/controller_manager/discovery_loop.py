"""
Discovery loop for ControllerManager.

Async task that handles:
- Controller discovery and tracking
- Battery monitoring
- State polling at fixed intervals
- LED updates

Uses asyncio.to_thread() for blocking USB I/O operations to avoid
blocking the main event loop.

Note: RSSI monitoring is handled by the host pairing-daemon which has
direct access to hcitool for reliable signal strength readings.
"""

import asyncio
import contextlib
import logging
import math
import time
from typing import TYPE_CHECKING

from opentelemetry import context as otel_context
from opentelemetry import trace

from lib.controller_constants import (
    AxisKey,
    ButtonKey,
    ControllerInfoKey,
    StateKey,
)
from lib.telemetry import SpanAttr, get_tracer
from services.controller_manager import metrics

if TYPE_CHECKING:
    from services.controller_manager.backend import ControllerBackend
    from services.controller_manager.button_detector import ButtonDetector
    from services.controller_manager.discovery import PeriodicRescanTimer
    from services.controller_manager.event_publisher import EventPublisher
    from services.controller_manager.feedback_manager import FeedbackManager
    from services.controller_manager.monitoring import ControllerMonitoring
    from services.controller_manager.name_manager import NameManager
    from services.controller_manager.state_cache import StateCache

logger = logging.getLogger(__name__)

# Lazy telemetry initialization - defers OTLP setup until first span
tracer = get_tracer(__name__)


def visible_serials(tracked_controllers: dict[str, dict]) -> list[str]:
    """Return serials that button-stream consumers (the menu) may learn about.

    Reserved controllers (#777) are excluded — they are never announced via
    connect/disconnect events nor included in the connected_serials roster.
    """
    return [serial for serial, info in tracked_controllers.items() if not info.get(ControllerInfoKey.RESERVED, False)]


# Button keys checked every poll cycle — tuple to avoid per-call list allocation
_BUTTON_KEYS = (
    ButtonKey.MOVE,
    ButtonKey.TRIGGER,
    ButtonKey.PS,
    ButtonKey.CROSS,
    ButtonKey.CIRCLE,
    ButtonKey.SQUARE,
    ButtonKey.TRIANGLE,
    ButtonKey.SELECT,
    ButtonKey.START,
)


class DiscoveryLoop:
    """
    Async discovery task for controller management.

    Handles controller discovery, state polling, battery monitoring,
    and LED updates as an async task on the main event loop.

    Uses fixed interval scheduling for consistent timing and
    asyncio.to_thread() for blocking USB I/O operations.
    """

    def __init__(
        self,
        backend: "ControllerBackend",
        tracked_controllers: dict[str, dict],
        controller_states: dict[str, dict],
        button_detector: "ButtonDetector",
        state_cache_manager: "StateCache",
        feedback_manager: "FeedbackManager",
        monitoring: "ControllerMonitoring",
        rescan_timer: "PeriodicRescanTimer",
        paired_serials: list[str],
        base_colors: dict[str, tuple[int, int, int]],
        event_publisher: "EventPublisher",
        name_manager: "NameManager | None" = None,
    ):
        """
        Initialize the discovery loop.

        Args:
            backend: Controller backend implementation
            tracked_controllers: Dict of serial -> controller info
            controller_states: Dict of serial -> state dict
            button_detector: Button transition detector
            state_cache_manager: State caching manager
            feedback_manager: LED/vibration feedback manager
            monitoring: Battery monitoring
            rescan_timer: Periodic rescan timer
            paired_serials: List of paired controller serials
            base_colors: Dict of serial -> base LED color
            event_publisher: Event publisher for cross-thread communication
            name_manager: Name manager for human-readable controller names (Issue #7)
        """
        self.backend = backend
        self.tracked_controllers = tracked_controllers
        self.controller_states = controller_states
        self.button_detector = button_detector
        self.state_cache_manager = state_cache_manager
        self.feedback_manager = feedback_manager
        self.monitoring = monitoring
        self.rescan_timer = rescan_timer
        self.paired_serials = paired_serials
        self.base_colors = base_colors
        self.event_publisher = event_publisher
        self.name_manager = name_manager

        # Discovery task state
        self.running = True
        self.backend_initialized = False
        self._task: asyncio.Task | None = None
        self._initialized_event: asyncio.Event | None = None

        # LED update timing (Phase 72: separated from polling)
        self._last_led_update = 0.0

        # Discovery check throttle: 1Hz is sufficient for detecting
        # connect/disconnect — no need to run at 100Hz polling rate
        self._last_discovery_check = 0.0
        self._discovery_interval = 1.0  # seconds

        # Fixed interval polling configuration - always 100Hz
        # There's always either a menu or game stream active, so no need for idle mode
        self._poll_interval = 0.010  # 100Hz

        # Activity tracking for metrics
        self._last_activity_time: dict[str, float] = {}
        self._previous_accel: dict[str, tuple[float, float, float]] = {}
        self._idle_threshold_seconds = 5.0
        self._accel_movement_threshold = 0.05

        # Cached metric label children (Issue #542: avoid 10K+ label lookups/sec)
        # Populated at spawn, cleaned up on disconnect
        self._accel_gauges: dict[str, dict] = {}

        # Cached serial list — rebuilt on connect/disconnect, reused at 100Hz
        self._cached_serials: list[str] | None = None

        # Previous active/idle counts — only update gauge on change
        self._prev_active_count = -1
        self._prev_idle_count = -1

        # Debug logging throttle
        self._last_controller_count_log = 0.0

        # Health counters: accumulated between stream frames, reset on drain
        self._poll_drops: dict[str, int] = {}
        self._poll_errors: dict[str, int] = {}

        # Bluetooth-health window accumulation (#732 M3). The window is the
        # ~1s span-emission interval, decoupled from the 100Hz discovery cadence.
        # The 100Hz path only increments cheap counters; gap/ratio/hz are derived
        # once per window at emission time.
        self._health_interval = 1.0  # seconds between health spans/metrics
        # Emit gate uses monotonic (immune to wall-clock jumps); window/gap math
        # uses wall-clock time.time() to match the poll loop's current_time.
        self._last_health_emit = time.monotonic()
        self._window_start = time.time()
        # Per-serial counters reset every window.
        self._hw_polls: dict[str, int] = {}  # polls attempted
        self._hw_fresh: dict[str, int] = {}  # polls returning a genuinely-new frame
        self._hw_drops: dict[str, int] = {}  # polls returning None
        self._hw_max_gap_ms: dict[str, float] = {}  # max gap between fresh frames
        self._hw_last_fresh_ts: dict[str, float] = {}  # monotonic ts of last fresh frame
        # Identity of the last poll object per serial — distinguishes a stale
        # replay (same object, e.g. UnstableAdapter throttle window) from a fresh
        # frame. The poll dict carries no sequence/timestamp, so identity is the
        # cheapest reliable freshness signal.
        self._hw_last_obj_id: dict[str, int] = {}

        # Gameplay disconnect events: accumulated between stream frames, drained by servicer (#580)
        self._gameplay_disconnects: list[str] = []

    def start(self) -> None:
        """Start the discovery task.

        Must be called from an async context (after event loop is running).
        """
        self._initialized_event = asyncio.Event()
        self._task = asyncio.create_task(self._discovery_loop())
        logger.info("Discovery loop started as async task")

    async def wait_initialized(self, timeout_seconds: float = 10.0) -> bool:
        """Wait for the backend to be initialized.

        Args:
            timeout_seconds: Maximum time to wait for initialization.

        Returns:
            True if initialized successfully, False if timed out or failed.
        """
        if self._initialized_event is None:
            return False
        try:
            await asyncio.wait_for(self._initialized_event.wait(), timeout=timeout_seconds)
            return self.backend_initialized
        except TimeoutError:
            logger.error(f"Backend initialization timed out after {timeout_seconds}s")
            return False

    def stop(self) -> None:
        """Stop the discovery loop."""
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def wait_stopped(self, timeout_seconds: float | None = None) -> None:
        """Wait for the discovery task to complete."""
        if self._task:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout_seconds)

    async def _discovery_loop(self) -> None:
        """
        Async task for controller discovery and battery monitoring.

        Uses fixed interval scheduling for consistent timing:
        - Gameplay mode (streams active): 100Hz polling for fast button detection
        - Idle mode (no streams): 10Hz polling to save resources

        Blocking USB I/O operations are run via asyncio.to_thread() to avoid
        blocking the main event loop.

        Phase 56: Event-driven spans - Only creates spans for actual events (controller connected),
        not for routine polling. Metrics track polling operations.
        Phase 57: Uses backend abstraction for platform independence.
        """
        try:
            # Initialize backend
            self.backend_initialized = await self.backend.initialize()
            if not self.backend_initialized:
                logger.error("Backend initialization failed - discovery loop will not run")
                if self._initialized_event:
                    self._initialized_event.set()  # Signal even on failure so waiters don't hang
                return
            logger.info("Backend initialized successfully")
            if self._initialized_event:
                self._initialized_event.set()
        except Exception as e:
            logger.error(f"Failed to initialize backend: {e}", exc_info=True)
            if self._initialized_event:
                self._initialized_event.set()  # Signal even on failure so waiters don't hang
            return

        # Fixed interval scheduling
        next_poll_time = time.monotonic()

        # Set constant polling metrics once (not every 10ms)
        metrics.polling_mode.set(1)  # Always in "gameplay mode" now
        metrics.polling_target_hz.set(100)

        while self.running:
            try:
                current_time = time.time()

                # Fixed 100Hz polling - there's always either a menu or game stream active
                poll_interval = self._poll_interval

                # Check for new controllers at 1Hz (not every poll cycle).
                # Discovery involves gRPC calls + USB enumeration — running it
                # at 100Hz wastes CPU and causes lag on the rust-hid service.
                if current_time - self._last_discovery_check >= self._discovery_interval:
                    with metrics.discovery_check_duration_seconds.time():
                        await self._check_for_new_controllers()
                        metrics.discovery_checks_total.inc()
                    self._last_discovery_check = current_time

                # Update controller states from backend
                await self._update_controller_states()

                # Bluetooth health: emit a ~1Hz span + metrics from accumulated
                # window counters. Decoupled from discovery cadence (#732 M3).
                now_mono = time.monotonic()
                if now_mono - self._last_health_emit >= self._health_interval:
                    self._emit_bluetooth_health(time.time())
                    self._last_health_emit = now_mono

                # Check battery levels every 30 seconds (Phase 39 - Task 4, Phase 70)
                # Note: Battery display/warnings moved to menu service (Phase 70)
                if current_time - self.monitoring.last_battery_check >= 30.0:
                    with metrics.battery_check_duration_seconds.time():
                        self.monitoring.check_battery_levels(self.tracked_controllers)
                        metrics.battery_checks_total.inc()
                    self.monitoring.last_battery_check = current_time

                # Phase 72: Update LEDs at 20Hz (every 50ms) - separated from polling
                if current_time - self._last_led_update >= 0.05:
                    # Run in thread pool since update_all_leds() has blocking USB calls
                    updated_count = await asyncio.to_thread(self.backend.update_all_leds)
                    self._last_led_update = current_time
                    # Track LED batch update efficiency
                    metrics.led_batch_updates_total.inc()
                    metrics.led_controllers_updated_per_batch.observe(updated_count)

                # Fixed interval sleep - precise timing using monotonic clock
                next_poll_time += poll_interval
                sleep_time = next_poll_time - time.monotonic()

                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                else:
                    # Behind schedule - reset to avoid spiral
                    next_poll_time = time.monotonic()

            except asyncio.CancelledError:  # NOSONAR — intentional: graceful shutdown of discovery loop
                logger.info("Discovery loop cancelled")
                break
            except Exception as e:
                logger.error(f"Discovery loop error: {e}", exc_info=True)
                await asyncio.sleep(5.0)

    async def _check_for_new_controllers(self) -> None:
        """
        Check for newly connected controllers (hardware).

        Phase 56: Event-driven spans - Only creates spans when NEW controllers are discovered.
        Routine checks are tracked via metrics only (no spans).
        Phase 57: Uses backend abstraction instead of direct psmove.
        Phase 79: Periodic forced rescan to catch externally paired controllers.
        """
        if not self.backend_initialized:
            return

        try:
            # Phase 79: Check if it's time for a forced rescan
            force_rescan = self.rescan_timer.should_force_rescan()

            # Get list of connected controllers from backend
            # Run in thread pool since this has blocking USB calls.
            # Wrap in a span so HID service spans are linked via trace context.
            # Capture the span's context and attach it in the thread — OTEL
            # context doesn't automatically cross asyncio.to_thread() boundaries.
            with tracer.start_as_current_span("controller.discover") as span:
                span.set_attribute("discover.force_rescan", force_rescan)
                ctx = otel_context.get_current()

                def _discover_with_context(force: bool) -> list[str]:
                    token = otel_context.attach(ctx)
                    try:
                        return self.backend.get_connected_controllers(force)
                    finally:
                        otel_context.detach(token)

                connected_serials = await asyncio.to_thread(_discover_with_context, force_rescan)
            connected_set = set(connected_serials)

            # Check for disconnected controllers (in tracked but not in connected)
            tracked_serials = set(self.tracked_controllers.keys())

            disconnected_serials = tracked_serials - connected_set
            for serial in disconnected_serials:
                logger.info(f"Controller {serial} disconnected - cleaning up server tracking")
                # Invalidate cached serial list
                self._cached_serials = None
                # Capture name + reservation before removing from tracked_controllers
                name = ""
                reserved = False
                if serial in self.tracked_controllers:
                    name = self.tracked_controllers[serial].get(ControllerInfoKey.NAME, "")
                    reserved = self.tracked_controllers[serial].get(ControllerInfoKey.RESERVED, False)
                    del self.tracked_controllers[serial]
                if serial in self.controller_states:
                    del self.controller_states[serial]
                self.state_cache_manager.clear_controller(serial)
                self.button_detector.clear_controller(serial)
                # Note: Keep base_colors[serial] so we can restore on reconnect!
                # Activity tracking cleanup
                self._last_activity_time.pop(serial, None)
                self._previous_accel.pop(serial, None)
                self._accel_gauges.pop(serial, None)
                self._poll_drops.pop(serial, None)
                self._poll_errors.pop(serial, None)
                # Bluetooth-health window counters
                self._hw_polls.pop(serial, None)
                self._hw_fresh.pop(serial, None)
                self._hw_drops.pop(serial, None)
                self._hw_max_gap_ms.pop(serial, None)
                self._hw_last_fresh_ts.pop(serial, None)
                self._hw_last_obj_id.pop(serial, None)
                # Clear per-serial health metric labels so disconnected
                # controllers don't linger in dashboards.
                metrics.controller_bluetooth_dropped_events_ratio.remove(serial)
                metrics.controller_bluetooth_movement_update_hz.remove(serial)
                # Publish disconnect event to button event stream subscribers —
                # but never announce reserved controllers to the menu (#777).
                if not reserved:
                    self.button_detector.publish_connection_event(
                        serial,
                        is_connect=False,
                        name=name,
                        connected_serials=visible_serials(self.tracked_controllers),
                    )
                # Queue disconnect for gameplay stream subscribers (#580)
                self._gameplay_disconnects.append(serial)
                metrics.controller_disconnect_total.labels(serial=serial).inc()
                metrics.controller_connected.labels(serial=serial).set(0)

            # Check for new controllers
            for serial in connected_serials:
                if serial not in self.tracked_controllers:
                    # Invalidate cached serial list
                    self._cached_serials = None
                    # New controller found - create event span
                    with tracer.start_as_current_span("controller_connected") as span:
                        span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)
                        span.set_attribute("controller.count_total", len(connected_serials))

                        logger.info(f"Discovered new controller: {serial}")

                        # Spawn tracking process
                        with tracer.start_as_current_span("spawn_controller_process") as spawn_span:
                            spawn_span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)
                            await self._spawn_controller_process(serial)

                        # Publish connect event to button event stream subscribers —
                        # but never announce reserved controllers to the menu (#777).
                        info = self.tracked_controllers.get(serial, {})
                        battery = info.get(ControllerInfoKey.BATTERY, 0)
                        name = info.get(ControllerInfoKey.NAME, "")
                        if not info.get(ControllerInfoKey.RESERVED, False):
                            self.button_detector.publish_connection_event(
                                serial,
                                is_connect=True,
                                battery=battery,
                                name=name,
                                connected_serials=visible_serials(self.tracked_controllers),
                            )

                        # Restore base color if we had one before (reconnection case)
                        if serial in self.base_colors:
                            color = self.base_colors[serial]
                            logger.info(f"Restoring base color for reconnected controller {serial}: {color}")
                            await self.feedback_manager.set_controller_color(serial, color)

        except Exception as e:
            logger.error(f"Error discovering controllers: {e}", exc_info=True)

    async def _update_controller_states(self) -> None:
        """
        Update states for all tracked controllers from backend.

        Polls all controllers in parallel using asyncio.gather().
        Called at fixed intervals (100Hz gameplay, 10Hz idle) for consistent timing.
        """
        if not self.backend_initialized:
            return

        try:
            # Get list of serials (cached, rebuilt on connect/disconnect)
            all_serials = self._cached_serials
            if all_serials is None:
                all_serials = list(self.tracked_controllers.keys())
                self._cached_serials = all_serials

            if not all_serials:
                return

            current_time = time.time()

            # Count active vs idle controllers for metrics (based on recent activity)
            active_count = 0
            idle_count = 0
            for serial in all_serials:
                last_activity = self._last_activity_time.get(serial, current_time)
                is_idle = (current_time - last_activity) > self._idle_threshold_seconds
                if is_idle:
                    idle_count += 1
                else:
                    active_count += 1

            # Update activity metrics only when values change
            if active_count != self._prev_active_count:
                metrics.adaptive_polling_active_controllers.set(active_count)
                self._prev_active_count = active_count
            if idle_count != self._prev_idle_count:
                metrics.adaptive_polling_idle_controllers.set(idle_count)
                self._prev_idle_count = idle_count

            # Debug: Log tracked controller count periodically (every 5 seconds)
            if current_time - self._last_controller_count_log >= 5.0:
                adapter_info = ""
                if hasattr(self.backend, "get_adapter_type"):
                    adapter_counts: dict[str, int] = {}
                    for s in all_serials:
                        atype = self.backend.get_adapter_type(s)
                        adapter_counts[atype] = adapter_counts.get(atype, 0) + 1
                    adapter_info = ", adapters=" + "+".join(f"{k}:{v}" for k, v in sorted(adapter_counts.items()))
                msg = f"Polling {len(all_serials)} controllers at 100Hz"
                msg += f", active={active_count}, idle={idle_count}{adapter_info}"
                logger.info(msg)
                self._last_controller_count_log = current_time

            # Poll all controllers — direct loop instead of asyncio.gather()
            # Post-PR #677, get_controller_state() is async but does zero I/O
            # (just a dict read from adapter._latest_data), so gather() overhead
            # (N coroutine objects + list + internal machinery) is pure waste.
            start_time = time.time()

            results = []
            for serial in all_serials:
                try:
                    results.append(await self.backend.get_controller_state(serial))
                except Exception as e:
                    results.append(e)

            # Record metrics
            poll_duration = time.time() - start_time
            metrics.poll_batch_duration_seconds.observe(poll_duration)
            metrics.poll_batch_size.observe(len(all_serials))

            # Process all results
            for serial, state in zip(all_serials, results, strict=False):
                # Skip if controller was removed during polling
                if serial not in self.tracked_controllers:
                    continue

                # Bluetooth-health: every processed serial counts as one attempt.
                self._hw_polls[serial] = self._hw_polls.get(serial, 0) + 1

                # Handle exceptions from individual controller reads
                if isinstance(state, Exception):
                    logger.debug(f"Error updating state for {serial}: {state}")
                    self._poll_errors[serial] = self._poll_errors.get(serial, 0) + 1
                    metrics.controller_poll_errors_total.labels(serial=serial).inc()
                    # An exception yields no fresh frame — count as a drop for health.
                    self._hw_drops[serial] = self._hw_drops.get(serial, 0) + 1
                    continue

                if not state:
                    self._poll_drops[serial] = self._poll_drops.get(serial, 0) + 1
                    metrics.controller_poll_drops_total.labels(serial=serial).inc()
                    self._hw_drops[serial] = self._hw_drops.get(serial, 0) + 1
                    continue

                # Truthy dict: fresh frame only if the poll returned a *different*
                # object than last time. A stale replay (same object, e.g. an
                # UnstableAdapter throttle window) is NOT a new movement update.
                obj_id = id(state)
                if self._hw_last_obj_id.get(serial) != obj_id:
                    self._hw_last_obj_id[serial] = obj_id
                    self._record_fresh_frame(serial, current_time)
                else:
                    # Stale replay: no new frame, but not a hardware drop either.
                    # Counts against the update rate via _hw_polls without bumping
                    # _hw_fresh, so movement_update_hz reflects the degradation.
                    pass

                # Update stored state
                self.controller_states[serial] = state

                # Update battery in tracked_controllers info
                if StateKey.BATTERY in state:
                    self.tracked_controllers[serial][ControllerInfoKey.BATTERY] = state[StateKey.BATTERY]

                # Detect button transitions immediately after polling (not in gRPC handlers)
                # This ensures button events are detected at polling frequency, not stream frequency
                self.button_detector.detect_transitions_from_state(serial, state, self.tracked_controllers)

                # Track activity for metrics
                self._update_activity_tracking(serial, state, current_time)

        except Exception as e:
            logger.error(f"Error updating controller states: {e}", exc_info=True)

    def _update_activity_tracking(self, serial: str, state: dict, current_time: float) -> None:
        """
        Update activity tracking for adaptive polling.

        Activity is detected when:
        - Any button is pressed
        - Accelerometer values change significantly (movement detected)

        Issue #62: Also records acceleration metrics for 100Hz visualization.
        """
        activity_detected = False

        # Check for any button activity
        for key in _BUTTON_KEYS:
            if state.get(key, False):
                activity_detected = True
                break

        # Check for accelerometer movement if no button pressed
        if StateKey.ACCEL in state:
            accel = state[StateKey.ACCEL]
            ax = accel.get(AxisKey.X, 0)
            ay = accel.get(AxisKey.Y, 0)
            az = accel.get(AxisKey.Z, 0)
            current_accel = (ax, ay, az)

            # Issue #62: Record acceleration metrics for 100Hz visualization
            # Calculate magnitude for easier threshold-based alerts
            magnitude = math.sqrt(ax * ax + ay * ay + az * az)

            # Issue #542: Use cached label children (avoids 5× label lookup per poll)
            gauges = self._accel_gauges.get(serial)
            if gauges:
                gauges["magnitude"].set(magnitude)
                gauges["prom_magnitude"].set(magnitude)
                gauges["x"].set(ax)
                gauges["y"].set(ay)
                gauges["z"].set(az)

            prev_accel = self._previous_accel.get(serial)
            if prev_accel and not activity_detected:
                # Calculate magnitude of acceleration change
                dx = abs(current_accel[0] - prev_accel[0])
                dy = abs(current_accel[1] - prev_accel[1])
                dz = abs(current_accel[2] - prev_accel[2])
                movement = dx + dy + dz

                if movement > self._accel_movement_threshold:
                    activity_detected = True

            self._previous_accel[serial] = current_accel

        # Update last activity time if activity detected
        if activity_detected:
            self._last_activity_time[serial] = current_time

    def _record_fresh_frame(self, serial: str, now: float) -> None:
        """Record a genuinely-new frame for the bluetooth-health window.

        Tracks the fresh-frame count and the max inter-event gap (ms) between
        consecutive fresh frames. Cheap — runs on the 100Hz path. ``now`` is a
        wall-clock timestamp (seconds); only deltas are used so the clock source
        just needs to be monotone within a window.
        """
        self._hw_fresh[serial] = self._hw_fresh.get(serial, 0) + 1
        last_ts = self._hw_last_fresh_ts.get(serial)
        if last_ts is not None:
            gap_ms = (now - last_ts) * 1000.0
            if gap_ms > self._hw_max_gap_ms.get(serial, 0.0):
                self._hw_max_gap_ms[serial] = gap_ms
        self._hw_last_fresh_ts[serial] = now

    def _compute_health_window(self, window_seconds: float) -> dict:
        """Derive per-serial and window-aggregate health signals.

        Called once per window at emission time. Returns a dict with:
          * ``per_serial``: {serial: {update_hz, dropped_pct, max_gap_ms}}
          * window aggregates: ``event_gap_ms`` (max), ``dropped_events_pct``
            (total drops / total polls), ``movement_update_hz`` (min across
            serials), ``active_controllers`` (count of serials polled).

        ``window_seconds`` guards against a zero/negative interval (clamped to a
        small positive value) so rate math never divides by zero.
        """
        duration = max(window_seconds, 1e-6)
        per_serial: dict[str, dict] = {}
        serials = list(self._hw_polls.keys())

        total_events = 0  # event-bearing cycles: fresh + dropped
        total_drops = 0
        max_gap_ms = 0.0
        min_hz: float | None = None

        for serial in serials:
            fresh = self._hw_fresh.get(serial, 0)
            drops = self._hw_drops.get(serial, 0)
            gap_ms = self._hw_max_gap_ms.get(serial, 0.0)

            update_hz = fresh / duration
            # Dropped-event ratio: drops as a fraction of event-bearing poll
            # cycles (fresh frames + drops). Stale replays are excluded — they
            # are the *absence* of an event, not a dropped event, so they must
            # not dilute the denominator. This matches the UnstableAdapter
            # design (every Nth *fresh* frame dropped) so the ratio reflects the
            # genuine event-loss rate rather than washing out across 100Hz polls.
            events = fresh + drops
            dropped_pct = (drops / events) if events else 0.0

            per_serial[serial] = {
                "update_hz": update_hz,
                "dropped_pct": dropped_pct,
                "max_gap_ms": gap_ms,
            }

            total_events += events
            total_drops += drops
            if gap_ms > max_gap_ms:
                max_gap_ms = gap_ms
            if min_hz is None or update_hz < min_hz:
                min_hz = update_hz

        return {
            "per_serial": per_serial,
            "event_gap_ms": max_gap_ms,
            "dropped_events_pct": (total_drops / total_events) if total_events else 0.0,
            "movement_update_hz": min_hz if min_hz is not None else 0.0,
            "active_controllers": len(serials),
        }

    def _reset_health_window(self, now: float) -> None:
        """Clear per-serial window counters and start a new window at ``now``."""
        self._hw_polls.clear()
        self._hw_fresh.clear()
        self._hw_drops.clear()
        self._hw_max_gap_ms.clear()
        # Keep _hw_last_fresh_ts / _hw_last_obj_id across windows so the first
        # fresh frame of the next window measures its gap from the real previous
        # frame, and stale replays spanning the boundary are still detected.
        self._window_start = now

    def _emit_bluetooth_health(self, now: float) -> None:
        """Emit the ~1Hz controller.bluetooth_health span + health metrics.

        Decoupled from discovery cadence via ``_last_health_emit``. Skips
        emission entirely when no controllers were polled this window — an idle
        lobby with no controllers produces no health heartbeat (the agent's
        OBSERVE step treats absence-of-span as "nothing to perceive").
        """
        window_seconds = now - self._window_start
        signals = self._compute_health_window(window_seconds)

        # Skip-when-empty: no point spamming empty health spans with no controllers.
        if signals["active_controllers"] == 0:
            self._reset_health_window(now)
            return

        # Window-level rollout summary (target backend + cohort size).
        target_backend = ""
        rollout_count = 0
        if hasattr(self.backend, "_rollout_router"):
            try:
                target_backend, rollout_count = self.backend._rollout_router.current_target()
            except Exception:
                logger.debug("Failed to read rollout summary for health span", exc_info=True)

        # --- Span: periodic root span, like controller.discover ---------------
        with tracer.start_as_current_span("controller.bluetooth_health") as span:
            span.set_attribute("bluetooth.event_gap_ms", float(signals["event_gap_ms"]))
            span.set_attribute("bluetooth.dropped_events_pct", float(signals["dropped_events_pct"]))
            span.set_attribute("bluetooth.movement_update_hz", float(signals["movement_update_hz"]))
            span.set_attribute("bluetooth.active_controllers", int(signals["active_controllers"]))
            span.set_attribute("bluetooth.target_backend", target_backend)
            span.set_attribute("bluetooth.rollout_count", int(rollout_count))

            # One event per active serial with per-serial signals.
            for serial, s in signals["per_serial"].items():
                adapter_type = ""
                if hasattr(self.backend, "get_adapter_type"):
                    adapter_type = self.backend.get_adapter_type(serial)
                span.add_event(
                    "bluetooth_controller_sample",
                    {
                        "controller.serial": serial,
                        "bluetooth.movement_update_hz": float(s["update_hz"]),
                        "bluetooth.dropped_events_pct": float(s["dropped_pct"]),
                        "controller.adapter": adapter_type,
                    },
                )

        # --- Metrics ----------------------------------------------------------
        # Window-max gap in SECONDS (repo convention for *_seconds histograms).
        metrics.controller_bluetooth_event_gap_seconds.observe(signals["event_gap_ms"] / 1000.0)
        metrics.controller_bluetooth_dropped_events_ratio_window.set(signals["dropped_events_pct"])
        for serial, s in signals["per_serial"].items():
            metrics.controller_bluetooth_dropped_events_ratio.labels(serial=serial).set(s["dropped_pct"])
            metrics.controller_bluetooth_movement_update_hz.labels(serial=serial).set(s["update_hz"])
            # Populate the previously-unset generic update-rate gauge too.
            metrics.controller_state_update_hz.labels(serial=serial).set(s["update_hz"])

        self._reset_health_window(now)

    def drain_health_counters(self, serial: str) -> tuple[int, int, int]:
        """Drain and reset health counters for a controller.

        Called by servicer when building each stream frame. Returns accumulated
        counts since the last drain and resets them to zero.

        Safe without locks — poll loop and stream frame building both run on
        the same asyncio event loop.

        Returns:
            Tuple of (poll_drops, poll_errors, led_failures).
        """
        drops = self._poll_drops.pop(serial, 0)
        errors = self._poll_errors.pop(serial, 0)
        led = 0
        if hasattr(self.backend, "_led_failures"):
            led = self.backend._led_failures.pop(serial, 0)
        return drops, errors, led

    def drain_disconnects(self) -> list[str]:
        """Drain and return accumulated gameplay disconnect events.

        Called by servicer when building each stream frame. Returns serials
        that disconnected since the last drain and clears the list.

        Safe without locks -- discovery loop and stream frame building both
        run on the same asyncio event loop.

        Returns:
            List of serial numbers that disconnected since last drain.
        """
        if not self._gameplay_disconnects:
            return []
        disconnects = self._gameplay_disconnects
        self._gameplay_disconnects = []
        return disconnects

    async def _spawn_controller_process(self, serial: str) -> None:
        """
        Start tracking a newly connected controller.

        Phase 57: Simplified to use backend for state tracking.
        """
        try:
            # Get initial state from backend (retry briefly — hidraw non-blocking
            # read may return empty immediately after open_path)
            state = None
            for _attempt in range(5):
                state = await self.backend.get_controller_state(serial)
                if state:
                    break
                await asyncio.sleep(0.02)  # 20ms between retries

            if not state:
                logger.error(f"Failed to get initial state for controller {serial}")
                return

            battery = state.get(StateKey.BATTERY, 5)

            # Issue #7: Get human-readable name for controller
            name = ""
            if self.name_manager:
                name = self.name_manager.get_name(serial)

            # Resolve adapter type if backend supports it (MultiplexerBackend)
            adapter_type = ""
            if hasattr(self.backend, "get_adapter_type"):
                adapter_type = self.backend.get_adapter_type(serial)

            # Resolve reservation metadata if backend supports it. Reserved
            # controllers are hidden from button-stream consumers (the menu)
            # but stay fully usable via gameplay-data streams and explicit-serial
            # LED/feedback commands (#777).
            reserved = False
            tag = ""
            if hasattr(self.backend, "get_controller_metadata"):
                metadata = self.backend.get_controller_metadata(serial)
                if metadata:
                    reserved = metadata.get(ControllerInfoKey.RESERVED, False)
                    tag = metadata.get(ControllerInfoKey.TAG, "")

            # Track controller
            self.tracked_controllers[serial] = {
                ControllerInfoKey.SERIAL: serial,
                ControllerInfoKey.BATTERY: battery,
                ControllerInfoKey.TEAM: 0,
                ControllerInfoKey.CONNECTED_AT: time.time(),
                ControllerInfoKey.NAME: name,
                ControllerInfoKey.ADAPTER: adapter_type,
                ControllerInfoKey.RESERVED: reserved,
                ControllerInfoKey.TAG: tag,
            }

            # Store initial state
            self.controller_states[serial] = state

            # Add attributes to current span (this is called within "spawn_controller_process" span)
            current_span = trace.get_current_span()
            current_span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)
            current_span.set_attribute("controller.battery", battery)
            current_span.set_attribute("controller.name", name)
            current_span.add_event(
                "controller_added_to_tracking",
                {"serial": serial, "battery": battery, "name": name},
            )

            logger.info(f"Started tracking controller {serial} ({name})")

            # Cache metric label children for this serial (Issue #542)
            # Avoids 5× _validate_labels + _make_key per poll cycle per controller
            self._accel_gauges[serial] = {
                "magnitude": metrics.controller_accel_magnitude.labels(serial=serial),
                "prom_magnitude": metrics.prom_controller_accel_magnitude.labels(serial=serial),
                "x": metrics.controller_accel_x.labels(serial=serial),
                "y": metrics.controller_accel_y.labels(serial=serial),
                "z": metrics.controller_accel_z.labels(serial=serial),
            }

            # Update metrics (Phase 38)
            metrics.active_controllers.inc()
            metrics.controller_connected.labels(serial=serial).set(1)
            metrics.controller_battery_level.labels(serial=serial).set(battery)
            metrics.controller_battery_pct.labels(serial=serial).set(metrics.battery_to_pct(battery))
            # Controller info metric with human-readable name (Issue #74)
            # Use actual name from name_manager, fallback to serial suffix
            if name:
                controller_name = name
            elif len(serial) >= 4:
                controller_name = f"PS Move {serial[-4:]}"
            else:
                controller_name = serial
            metrics.controller_info.labels(serial=serial, name=controller_name).set(1)
            # Initialize poll drop/error counters so rate() always returns a series,
            # even for controllers with zero drops (needed for dashboard queries).
            metrics.controller_poll_drops_total.labels(serial=serial)
            metrics.controller_poll_errors_total.labels(serial=serial)
            # Check if this is a reconnect
            if serial in self.paired_serials:
                metrics.controller_reconnect_total.labels(serial=serial).inc()

        except Exception as e:
            logger.error(f"Error starting controller tracking {serial}: {e}", exc_info=True)
