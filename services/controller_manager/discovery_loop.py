"""
Discovery loop for ControllerManager.

Async task that handles:
- State polling at a fixed 100 Hz (the hot path for death detection)
- Controller discovery and tracking (1 Hz)
- LED updates (20 Hz)
- Battery monitoring (30 s) and bluetooth-health emission (~1 Hz)

The poll runs alone in its loop; discovery, LED, battery and health work run as
separate periodic tasks so a slow step cannot delay the poll (#1447, NFR-002).
Blocking USB I/O runs via asyncio.to_thread() to keep the event loop free.

Note: RSSI monitoring is handled by the host pairing-daemon which has
direct access to hcitool for reliable signal strength readings.
"""

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
from services.controller_manager.connection_quality import QUALITY_LEVEL, GapStats, ReportGapWindow

if TYPE_CHECKING:
    from services.controller_manager.backend import ControllerBackend
    from services.controller_manager.button_detector import ButtonDetector
    from services.controller_manager.discovery import PeriodicRescanTimer
    from services.controller_manager.event_publisher import EventPublisher
    from services.controller_manager.feedback_manager import FeedbackManager
    from services.controller_manager.monitoring import ControllerMonitoring
    from services.controller_manager.name_manager import NameManager

logger = logging.getLogger(__name__)

# Lazy telemetry initialization - defers OTLP setup until first span
tracer = get_tracer(__name__)

# Minimum seconds between error lines of one failing periodic step (#1447).
_STEP_ERROR_LOG_INTERVAL = 10.0

# Silent link (UC-002 BR-007, #2466): a tracked controller with no fresh frame for
# `silent_disconnect_seconds` (controller flag set) counts as disconnected. The
# normal report gap is 20-50 ms (p95), so the 1 s floor is far above it.
SILENT_DISCONNECT_DEFAULT_S = 3.0
SILENT_DISCONNECT_MIN_S = 1.0
SILENT_DISCONNECT_MAX_S = 10.0

# A (re)registered controller's LED output is sent again this long after the
# registration, so a first write the device dropped while it was not ready yet
# is repeated (UC-002 BR-008, #2466).
LED_RESEND_DELAY_S = 0.5


def resolve_silent_disconnect_seconds(value: object) -> float:
    """The silent-link threshold from a flag value: a non-number or non-finite value gives 3 s, clamped to 1-10 s."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return SILENT_DISCONNECT_DEFAULT_S
    return min(SILENT_DISCONNECT_MAX_S, max(SILENT_DISCONNECT_MIN_S, float(value)))


def read_silent_disconnect_seconds() -> float:
    """Read `silent_disconnect_seconds` (controller set) live; any failure gives the 3 s default (flags.md §6)."""
    from lib.feature_flags import read_float_flag

    return resolve_silent_disconnect_seconds(
        read_float_flag("controller", "silent_disconnect_seconds", SILENT_DISCONNECT_DEFAULT_S)
    )


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


@dataclass
class _ControllerRecord:
    """Everything the loop keeps per connected controller.

    One record per serial replaces the per-serial dicts the loop used to clean up
    by hand: dropping the record on disconnect forgets all of it at once.
    """

    # Activity tracking for metrics
    last_activity_time: float | None = None
    previous_accel: tuple[float, float, float] | None = None
    # Cached metric label children (Issue #542: avoid 10K+ label lookups/sec),
    # populated at spawn.
    accel_gauges: dict | None = None
    # Health counters: accumulated between stream frames, reset on drain
    poll_drops: int = 0
    poll_errors: int = 0
    # Bluetooth-health window counters, reset every window. ``hw_polls`` > 0
    # means the serial was polled this window.
    hw_polls: int = 0  # polls attempted
    hw_fresh: int = 0  # polls returning a genuinely-new frame
    hw_drops: int = 0  # polls returning None
    hw_max_gap_ms: float = 0.0  # max gap between fresh frames
    # Kept across windows. Wall-clock (time.time()) ts of the last fresh frame.
    hw_last_fresh_ts: float | None = None
    # Identity of the last poll object — distinguishes a stale replay (same
    # object, e.g. UnstableAdapter throttle window) from a fresh frame. The poll
    # dict carries no sequence/timestamp, so identity is the cheapest reliable
    # freshness signal.
    hw_last_obj_id: int | None = None
    # Cumulative loop stall at the last fresh frame (bluetooth.event_gap_ms
    # excludes loop stalls, #1447).
    hw_stall_at_last: float | None = None
    # Connection quality (UC-035 BR-005): stats computed at the last health emission.
    connection_quality: GapStats | None = None


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
        feedback_manager: "FeedbackManager",
        monitoring: "ControllerMonitoring",
        rescan_timer: "PeriodicRescanTimer",
        paired_serials: list[str],
        base_colors: dict[str, tuple[int, int, int]],
        event_publisher: "EventPublisher",
        name_manager: "NameManager | None" = None,
        reconnect_color: "Callable[[str], tuple[int, int, int] | None] | None" = None,
    ):
        """
        Initialize the discovery loop.

        Args:
            backend: Controller backend implementation
            tracked_controllers: Dict of serial -> controller info
            controller_states: Dict of serial -> state dict
            button_detector: Button transition detector
            feedback_manager: LED/vibration feedback manager
            monitoring: Battery monitoring
            rescan_timer: Periodic rescan timer
            paired_serials: List of paired controller serials
            base_colors: Dict of serial -> base LED color
            event_publisher: Event publisher for cross-thread communication
            name_manager: Name manager for human-readable controller names (Issue #7)
            reconnect_color: Colour the LED gets when a controller (re)registers, asked from
                the current owner (UC-002 BR-008); None = the cached base colour
        """
        self.backend = backend
        self.tracked_controllers = tracked_controllers
        self.controller_states = controller_states
        self.button_detector = button_detector
        self.feedback_manager = feedback_manager
        self.monitoring = monitoring
        self.rescan_timer = rescan_timer
        self.paired_serials = paired_serials
        self.base_colors = base_colors
        self.event_publisher = event_publisher
        self.name_manager = name_manager
        self._reconnect_color = reconnect_color

        # Discovery task state
        self.running = True
        self.backend_initialized = False
        self._task: asyncio.Task | None = None
        # Discovery, LED, battery and health tasks, owned by _task (#1447)
        self._side_tasks: list[asyncio.Task] = []
        self._initialized_event: asyncio.Event | None = None

        # LED batch cadence (Phase 72: separated from polling; own task since #1447)
        self._led_interval = 0.05  # 20 Hz

        # Battery metrics refresh (Phase 39 / 70)
        self._battery_interval = 30.0

        # Discovery check throttle: 1Hz is sufficient for detecting
        # connect/disconnect — no need to run at 100Hz polling rate
        self._discovery_interval = 1.0  # seconds

        # Fixed interval polling configuration - always 100Hz
        # There's always either a menu or game stream active, so no need for idle mode
        self._poll_interval = 0.010  # 100Hz

        # One record per tracked controller; popped on disconnect.
        self._records: dict[str, _ControllerRecord] = {}
        self._idle_threshold_seconds = 5.0
        self._accel_movement_threshold = 0.05

        # Cached serial list — rebuilt on connect/disconnect, reused at 100Hz
        self._cached_serials: list[str] | None = None

        # Debug logging throttle
        self._last_controller_count_log = 0.0

        # Bluetooth-health window accumulation (#732 M3). The window is the
        # ~1s span-emission interval, decoupled from the 100Hz discovery cadence.
        # The 100Hz path only increments cheap counters; gap/ratio/hz are derived
        # once per window at emission time.
        self._health_interval = 1.0  # seconds between health spans/metrics
        # Window/gap math uses wall-clock time.time() to match the poll loop's
        # current_time; the emission schedule itself is monotonic (_run_periodic).
        self._window_start = time.time()
        # True while the rollout flags cannot be read (#1489): edge-triggers one
        # warning on entering and one info on leaving, not one log per window.
        self._rollout_read_failing = False
        # Connection quality (UC-035 BR-005): rolling 10 s window of fresh-frame
        # gaps per serial.
        self._gap_window = ReportGapWindow()
        # Monotonic pick-up time of the previous poll batch: lateness beyond the
        # poll interval is the loop's own stall, not the link's (UC-035 BR-005).
        self._last_pickup_mono: float | None = None
        # Cumulative loop stall (seconds); each record keeps its value at the last
        # fresh frame, so bluetooth.event_gap_ms excludes loop stalls too (#1447).
        self._loop_stall_total = 0.0

        # Gameplay disconnect events: accumulated between stream frames, drained by servicer (#580)
        self._gameplay_disconnects: list[str] = []

        # Controllers dropped for a silent link (UC-002 BR-007) that the backend still lists,
        # with the last sample they served: they register again once a poll returns another one.
        self._silent_serials: dict[str, object] = {}
        self._led_resend_delay = LED_RESEND_DELAY_S
        # Pending delayed LED resends (UC-002 BR-008), cancelled on disconnect and stop.
        self._led_resend_handles: dict[str, asyncio.TimerHandle] = {}

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
        for handle in self._led_resend_handles.values():
            handle.cancel()
        self._led_resend_handles.clear()
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

        Initializes the backend, starts the discovery / LED / battery / health
        side tasks and runs the fixed 100 Hz poll loop; the side tasks are
        cancelled and awaited when the poll loop ends (#1447).

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

        # Set constant polling metrics once (not every 10ms)
        metrics.polling_mode.set(1)  # Always in "gameplay mode" now
        metrics.polling_target_hz.set(100)

        # Discovery, LED batches, battery and bluetooth-health run as their own
        # tasks (#1447): a slow USB enumeration or LED write must never delay
        # the 100 Hz state poll that death detection depends on (NFR-002/004).
        self._side_tasks = [
            asyncio.create_task(self._run_periodic("discovery", self._discovery_interval, self._discovery_step)),
            asyncio.create_task(self._run_periodic("led", self._led_interval, self._led_step)),
            asyncio.create_task(self._run_periodic("battery", self._battery_interval, self._battery_step)),
            asyncio.create_task(
                self._run_periodic(
                    "bluetooth_health", self._health_interval, self._health_step, first_delay=self._health_interval
                )
            ),
        ]
        try:
            await self._poll_loop()
        finally:
            for task in self._side_tasks:
                task.cancel()
            await asyncio.gather(*self._side_tasks, return_exceptions=True)

    async def _poll_loop(self) -> None:
        """Fixed-cadence 100 Hz state poll; nothing else is awaited on this path."""
        next_poll_time = time.monotonic()
        while self.running:
            try:
                # Lateness of this poll against its schedule: event-loop
                # contention only, now that nothing else runs in this loop.
                metrics.poll_loop_lateness_seconds.observe(max(0.0, time.monotonic() - next_poll_time))

                await self._update_controller_states()

                # Fixed interval sleep - precise timing using monotonic clock
                next_poll_time += self._poll_interval
                sleep_time = next_poll_time - time.monotonic()

                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                else:
                    # Behind schedule - reset to avoid spiral
                    next_poll_time = time.monotonic()
                    await asyncio.sleep(0)  # still let the side tasks run

            except asyncio.CancelledError:  # NOSONAR — intentional: graceful shutdown of discovery loop
                logger.info("Discovery loop cancelled")
                break
            except Exception as e:
                logger.error(f"Discovery loop error: {e}", exc_info=True)
                await asyncio.sleep(5.0)
                next_poll_time = time.monotonic()

    async def _run_periodic(
        self,
        name: str,
        interval: float,
        step: Callable[[], Awaitable[None]],
        first_delay: float = 0.0,
    ) -> None:
        """Run ``step`` every ``interval`` seconds until the loop stops.

        A step that overruns its interval is followed immediately by the next
        one (no catch-up burst). Errors are logged and the schedule continues:
        the first failure with its traceback, then at most one line per
        ``_STEP_ERROR_LOG_INTERVAL`` with the count of failures in between, so
        a 20 Hz step that keeps failing does not flood the log. Cancellation
        propagates so the owner can await the task.
        """
        if first_delay > 0:
            await asyncio.sleep(first_delay)
        next_run = time.monotonic()
        last_error_log: float | None = None
        suppressed = 0
        while self.running:
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                now = time.monotonic()
                if last_error_log is None:
                    logger.error(f"Discovery loop {name} step error: {e}", exc_info=True)
                    last_error_log = now
                elif now - last_error_log >= _STEP_ERROR_LOG_INTERVAL:
                    logger.error(f"Discovery loop {name} step error: {e} ({suppressed} more since last log)")
                    last_error_log = now
                    suppressed = 0
                else:
                    suppressed += 1
            next_run += interval
            delay = next_run - time.monotonic()
            if delay <= 0:
                next_run = time.monotonic()
                delay = 0
            await asyncio.sleep(delay)

    async def _discovery_step(self) -> None:
        """Check for new / lost controllers (1 Hz)."""
        # Discovery involves gRPC calls + USB enumeration in a worker thread.
        with metrics.discovery_check_duration_seconds.time():
            await self._check_for_new_controllers()
            metrics.discovery_checks_total.inc()

    async def _led_step(self) -> None:
        """Flush LED colour / rumble changes and keep-alives (20 Hz)."""
        # Run in thread pool since update_all_leds() has blocking USB calls
        updated_count = await asyncio.to_thread(self.backend.update_all_leds)
        # Track LED batch update efficiency
        metrics.led_batch_updates_total.inc()
        metrics.led_controllers_updated_per_batch.observe(updated_count)

    async def _battery_step(self) -> None:
        """Refresh battery metrics (every 30 s; display/warnings live in the menu, Phase 70)."""
        with metrics.battery_check_duration_seconds.time():
            self.monitoring.check_battery_levels(self.tracked_controllers)
            metrics.battery_checks_total.inc()
        self.monitoring.last_battery_check = time.time()

    async def _health_step(self) -> None:
        """Emit the ~1 Hz bluetooth-health span + metrics (#732 M3)."""
        self._emit_bluetooth_health(time.time(), time.monotonic())

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
            # A silenced controller the backend no longer lists left for real: it registers
            # through the normal path when it comes back.
            for serial in [s for s in self._silent_serials if s not in connected_set]:
                del self._silent_serials[serial]

            # Disconnected: gone from the backend, or listed but silent (UC-002 A1, BR-007)
            silent = self._find_silent_links() & connected_set
            for serial in (set(self.tracked_controllers) - connected_set) | silent:
                if serial in silent:
                    state = self.controller_states.get(serial)
                    self._silent_serials[serial] = getattr(state, "sample", state)
                    logger.warning(f"Controller {serial} sent no report for its silent-link limit - disconnecting")
                    # Its route stays (the backend still lists it): drop the stale output (BR-008).
                    self.backend.clear_led_output(serial)
                else:
                    logger.info(f"Controller {serial} disconnected - cleaning up server tracking")
                self._drop_controller(serial)

            # Check for new controllers
            for serial in connected_serials:
                if serial in self.tracked_controllers:
                    continue
                if serial in self._silent_serials and not await self._reports_resumed(serial):
                    continue
                await self._register_controller(serial, len(connected_serials))

        except Exception as e:
            logger.error(f"Error discovering controllers: {e}", exc_info=True)

    def _drop_controller(self, serial: str) -> None:
        """Forget a disconnected controller and publish its disconnect (UC-002 A1)."""
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
        self.button_detector.clear_controller(serial)
        resend = self._led_resend_handles.pop(serial, None)
        if resend is not None:
            resend.cancel()
        # base_colors[serial] is kept; the owner decides the colour on reconnect (UC-002 BR-008).
        # Activity, poll counters, health window and connection quality
        # all live in the record.
        self._records.pop(serial, None)
        self._gap_window.forget(serial)
        # Clear per-serial health metric labels so disconnected
        # controllers don't linger in dashboards.
        metrics.controller_bluetooth_dropped_events_ratio.remove(serial)
        metrics.controller_bluetooth_movement_update_hz.remove(serial)
        _remove_quality_series(serial)
        # #1295: an untracked controller no longer exports its name.
        metrics.remove_controller_info(serial, name)
        # Publish disconnect event to button event stream subscribers —
        # but never announce reserved controllers to the menu (#777).
        # UC-002 BR-002: a reserved mock controller's disconnect is not published.
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

    def _find_silent_links(self) -> set[str]:
        """Tracked controllers with no fresh frame for the silent-link limit (UC-002 BR-007, #2466).

        Measured up to the last poll batch, not to now, and with the loop's own stalls
        subtracted: a poll loop that is late or stalled never makes a link look silent.
        Runs in the 1 Hz discovery step, so the 100 Hz poll pays nothing for it.
        """
        pickup = self._last_pickup_mono
        if pickup is None or not self.tracked_controllers:
            return set()
        limit = read_silent_disconnect_seconds()
        silent = set()
        for serial in self.tracked_controllers:
            age = self._gap_window.silent_seconds(serial, pickup)
            if age is not None and age >= limit:
                silent.add(serial)
        return silent

    async def _reports_resumed(self, serial: str) -> bool:
        """Whether a silenced controller serves a new sample again, so it registers again (UC-002 BR-007)."""
        state = await self.backend.get_controller_state(serial)
        if not state or getattr(state, "sample", state) is self._silent_serials[serial]:
            return False
        del self._silent_serials[serial]
        logger.info(f"Controller {serial} sends reports again - registering it")
        return True

    async def _register_controller(self, serial: str, connected_count: int) -> None:
        """Track a new or returning controller, publish its connect event and write its LED (UC-002 steps 3-5, A4)."""
        # Invalidate cached serial list
        self._cached_serials = None
        # New controller found - create event span
        with tracer.start_as_current_span("controller_connected") as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)
            span.set_attribute("controller.count_total", connected_count)

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
            # UC-002 BR-002: a reserved mock controller's connect is not published.
            if not info.get(ControllerInfoKey.RESERVED, False):
                self.button_detector.publish_connection_event(
                    serial,
                    is_connect=True,
                    battery=battery,
                    name=name,
                    connected_serials=visible_serials(self.tracked_controllers),
                )

            await self._restore_led(serial)

    async def _restore_led(self, serial: str) -> None:
        """Write a (re)registered controller's LED, never skipped as unchanged (UC-002 A4, BR-008, #2466).

        The colour comes from the current owner (``reconnect_color``: the game's view for
        its players, the cached lobby colour otherwise). The backend sends the output again
        on its next LED tick and once more after ``LED_RESEND_DELAY_S``, so a first write
        the re-opened device dropped is repeated.
        """
        if serial not in self.tracked_controllers:
            return  # the spawn failed; the next discovery pass retries
        self.backend.force_led_resend(serial)
        owner = self._reconnect_color
        color = owner(serial) if owner is not None else self.base_colors.get(serial)
        if color is not None:
            logger.info(f"Restoring LED colour for (re)registered controller {serial}: {color}")
            await self.feedback_manager.set_controller_color(serial, color)
        previous = self._led_resend_handles.pop(serial, None)
        if previous is not None:
            previous.cancel()
        self._led_resend_handles[serial] = asyncio.get_running_loop().call_later(
            self._led_resend_delay, self._resend_led_if_tracked, serial
        )

    def _resend_led_if_tracked(self, serial: str) -> None:
        """The delayed second LED send of ``_restore_led`` (UC-002 BR-008)."""
        self._led_resend_handles.pop(serial, None)
        if serial not in self.tracked_controllers:
            return
        try:
            self.backend.force_led_resend(serial)
        except Exception as e:
            logger.warning(f"Delayed LED resend for {serial} failed: {e}")

    async def _update_controller_states(self) -> None:
        """
        Update states for all tracked controllers from backend.

        Polls all controllers in parallel using asyncio.gather().
        Called at a fixed 100 Hz (no idle mode); the gameplay stream rate is set
        separately by the flag `game_loop.update_frequency_hz` (default 60 Hz; 30 Hz only when a
        client requests no rate).
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

            # Debug: Log tracked controller count periodically (every 5 seconds)
            if current_time - self._last_controller_count_log >= 5.0:
                self._log_controller_count(all_serials, current_time)

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

            # Pick-up time of this batch for the connection-quality window; any
            # lateness beyond one poll interval is the loop's own stall.
            pickup_mono = time.monotonic()
            self._note_batch_pickup(pickup_mono)

            # Record metrics
            poll_duration = time.time() - start_time
            metrics.poll_batch_duration_seconds.observe(poll_duration)
            metrics.poll_batch_size.observe(len(all_serials))

            # Process all results
            for serial, state in zip(all_serials, results, strict=False):
                # Skip if controller was removed during polling
                if serial not in self.tracked_controllers:
                    continue

                record = self._record(serial)
                # Bluetooth-health: every processed serial counts as one attempt.
                record.hw_polls += 1

                # Handle exceptions from individual controller reads
                if isinstance(state, Exception):
                    logger.debug(f"Error updating state for {serial}: {state}")
                    record.poll_errors += 1
                    metrics.controller_poll_errors_total.labels(serial=serial).inc()
                    # An exception yields no fresh frame — count as a drop for health.
                    record.hw_drops += 1
                    continue

                if not state:
                    record.poll_drops += 1
                    metrics.controller_poll_drops_total.labels(serial=serial).inc()
                    record.hw_drops += 1
                    continue

                # Truthy dict: fresh frame only if the poll returned a *different*
                # object than last time. A stale replay (same object, e.g. an
                # UnstableAdapter throttle window) is NOT a new movement update.
                # A tap-latch copy (press_latch.LatchedState) is judged by the real sample it copies,
                # so the copy is neither a fresh frame nor makes the next poll look like one (#2352 B3).
                # id() is only safe while the previous sample stays referenced (controller_states[serial], directly or
                # via LatchedState.sample, and the adapter's _last_state); dropping or copying them would reuse ids.
                obj_id = id(getattr(state, "sample", state))
                if record.hw_last_obj_id != obj_id:
                    record.hw_last_obj_id = obj_id
                    self._record_fresh_frame(serial, current_time, self._loop_stall_total)
                    self._gap_window.record(serial, pickup_mono)
                else:
                    # Stale replay: no new frame, but not a hardware drop either.
                    # Counts against the update rate via hw_polls without bumping
                    # hw_fresh, so movement_update_hz reflects the degradation.
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

    def _log_controller_count(self, all_serials: list[str], current_time: float) -> None:
        """The 5 s poll log: controller count, active / idle split (by recent activity) and adapters."""
        active_count = 0
        idle_count = 0
        for serial in all_serials:
            record = self._records.get(serial)
            last_activity = (
                record.last_activity_time if record and record.last_activity_time is not None else current_time
            )
            is_idle = (current_time - last_activity) > self._idle_threshold_seconds
            if is_idle:
                idle_count += 1
            else:
                active_count += 1

        adapter_info = ""
        adapter_counts: dict[str, int] = {}
        for s in all_serials:
            atype = self.backend.get_adapter_type(s)
            adapter_counts[atype] = adapter_counts.get(atype, 0) + 1
        if any(adapter_counts):  # a backend without adapters reports "" for all
            adapter_info = ", adapters=" + "+".join(f"{k}:{v}" for k, v in sorted(adapter_counts.items()))
        msg = f"Polling {len(all_serials)} controllers at 100Hz"
        msg += f", active={active_count}, idle={idle_count}{adapter_info}"
        logger.info(msg)
        self._last_controller_count_log = current_time

    def _note_batch_pickup(self, pickup_mono: float) -> None:
        """Feed the batch's pick-up lateness beyond one poll interval (the loop's own stall) to the window."""
        if self._last_pickup_mono is not None:
            stall = pickup_mono - self._last_pickup_mono - self._poll_interval
            self._gap_window.note_loop_stall(stall)
            if stall > 0:
                self._loop_stall_total += stall
        self._last_pickup_mono = pickup_mono

    def _update_activity_tracking(self, serial: str, state: dict, current_time: float) -> None:
        """
        Update activity tracking for adaptive polling.

        Activity is detected when:
        - Any button is pressed
        - Accelerometer values change significantly (movement detected)

        Issue #62: Also records acceleration metrics for 100Hz visualization.
        """
        record = self._record(serial)
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
            gauges = record.accel_gauges
            if gauges:
                gauges["magnitude"].set(magnitude)
                gauges["x"].set(ax)
                gauges["y"].set(ay)
                gauges["z"].set(az)

            prev_accel = record.previous_accel
            if prev_accel and not activity_detected:
                # Calculate magnitude of acceleration change
                dx = abs(current_accel[0] - prev_accel[0])
                dy = abs(current_accel[1] - prev_accel[1])
                dz = abs(current_accel[2] - prev_accel[2])
                movement = dx + dy + dz

                if movement > self._accel_movement_threshold:
                    activity_detected = True

            record.previous_accel = current_accel

        # Update last activity time if activity detected
        if activity_detected:
            record.last_activity_time = current_time

    def _record_fresh_frame(self, serial: str, now: float, stall_total: float = 0.0) -> None:
        """Record a genuinely-new frame for the bluetooth-health window.

        Tracks the fresh-frame count and the max inter-event gap (ms) between
        consecutive fresh frames. Cheap — runs on the 100Hz path. ``now`` is a
        wall-clock timestamp (seconds); only deltas are used so the clock source
        just needs to be monotone within a window. ``stall_total`` is the
        cumulative poll-loop stall: the part of a gap the loop itself ran late
        is not charged to the link (#1447, as in UC-035 BR-005).
        """
        record = self._record(serial)
        record.hw_fresh += 1
        last_ts = record.hw_last_fresh_ts
        if last_ts is not None:
            stalled = stall_total - (record.hw_stall_at_last if record.hw_stall_at_last is not None else stall_total)
            gap_ms = max(0.0, (now - last_ts - stalled) * 1000.0)
            if gap_ms > record.hw_max_gap_ms:
                record.hw_max_gap_ms = gap_ms
        record.hw_last_fresh_ts = now
        record.hw_stall_at_last = stall_total

    def sample_age_seconds(self, serial: str, now: float) -> float | None:
        """Seconds since the poll loop picked up ``serial``'s current sample.

        Feeds ``controller_input_lag_seconds`` at gameplay-frame send time. A
        stale replay keeps the old pick-up time, so a stalled backend shows as
        growing lag. ``None`` until the first fresh frame. ``now`` must be
        wall-clock (time.time()), matching the poll loop's timestamps.
        """
        record = self._records.get(serial)
        picked_up = record.hw_last_fresh_ts if record else None
        if picked_up is None:
            return None
        return max(0.0, now - picked_up)

    def connection_quality(self, serial: str) -> GapStats | None:
        """Connection quality of ``serial`` at the last health emission (UC-035 BR-005)."""
        record = self._records.get(serial)
        return record.connection_quality if record else None

    def _update_connection_quality(self, serials: list[str], now: float) -> None:
        """Refresh the rolling-window gap stats and their metrics for ``serials``."""
        for serial in serials:
            stats = self._gap_window.snapshot(serial, now)
            record = self._records.get(serial)
            if stats is None:
                if record is not None:
                    record.connection_quality = None
                _remove_quality_series(serial)
                continue
            if record is not None:
                record.connection_quality = stats
            metrics.controller_bluetooth_report_gap_p95_seconds.labels(serial=serial).set(stats.p95_ms / 1000.0)
            metrics.controller_bluetooth_report_gap_max_seconds.labels(serial=serial).set(stats.max_ms / 1000.0)
            metrics.controller_connection_quality.labels(serial=serial).set(QUALITY_LEVEL[stats.quality])

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
        polled = {serial: record for serial, record in self._records.items() if record.hw_polls > 0}
        serials = list(polled)

        total_events = 0  # event-bearing cycles: fresh + dropped
        total_drops = 0
        max_gap_ms = 0.0
        min_hz: float | None = None

        for serial, record in polled.items():
            fresh = record.hw_fresh
            drops = record.hw_drops
            gap_ms = record.hw_max_gap_ms

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
        for record in self._records.values():
            record.hw_polls = 0
            record.hw_fresh = 0
            record.hw_drops = 0
            record.hw_max_gap_ms = 0.0
        # Keep hw_last_fresh_ts / hw_last_obj_id across windows so the first
        # fresh frame of the next window measures its gap from the real previous
        # frame, and stale replays spanning the boundary are still detected.
        self._window_start = now

    def _log_rollout_read_transition(self, failing: bool) -> None:
        """Log entering / leaving the "rollout flags unreadable" state once each (#1489)."""
        if failing == self._rollout_read_failing:
            return
        self._rollout_read_failing = failing
        if failing:
            logger.warning("Rollout flags unreadable: health span reports rollout_read_error until they recover")
        else:
            logger.info("Rollout flags readable again: health span reports the rollout state")

    def _emit_bluetooth_health(self, now: float, now_mono: float | None = None) -> None:
        """Emit the ~1Hz controller.bluetooth_health span + health metrics.

        Runs on its own periodic task (#1447), decoupled from discovery. Skips
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

        # Window-level rollout summary (target backend + cohort size). A failed
        # read is NOT reported as "off" (""/0): target and count are omitted and
        # bluetooth.rollout_read_error is set, so the agent holds its rollout
        # state instead of treating a flagd hiccup as an operator act (#1489).
        target_backend = ""
        rollout_count = 0
        rollout_read_error = False
        try:
            target_backend, rollout_count = self.backend.rollout_summary()
        except Exception:
            rollout_read_error = True
            logger.debug("Failed to read rollout summary for health span", exc_info=True)
        self._log_rollout_read_transition(rollout_read_error)

        # Active rollout strategy ("off"/"immediate"/"progressive"). The agent
        # reads this to skip its count ladder under "immediate" routing. Empty
        # on any error so a flagd outage never breaks span emission; the agent
        # treats absence as "behave as before" (progressive ladder).
        rollout_strategy = ""
        try:
            rollout_strategy = self.backend.rollout_strategy()
        except Exception:
            logger.debug("Failed to read rollout strategy for health span", exc_info=True)

        self._update_connection_quality(
            list(signals["per_serial"]), now_mono if now_mono is not None else time.monotonic()
        )

        # --- Span: periodic root span, like controller.discover ---------------
        with tracer.start_as_current_span("controller.bluetooth_health") as span:
            span.set_attribute("bluetooth.event_gap_ms", float(signals["event_gap_ms"]))
            span.set_attribute("bluetooth.dropped_events_pct", float(signals["dropped_events_pct"]))
            span.set_attribute("bluetooth.movement_update_hz", float(signals["movement_update_hz"]))
            span.set_attribute("bluetooth.active_controllers", int(signals["active_controllers"]))
            if rollout_read_error:
                span.set_attribute("bluetooth.rollout_read_error", True)
            else:
                span.set_attribute("bluetooth.target_backend", target_backend)
                span.set_attribute("bluetooth.rollout_count", int(rollout_count))
                # The strategy read degrades to "off" on the same flagd error, so
                # it is only reported next to a successful rollout read (#1489).
                span.set_attribute("bluetooth.rollout_strategy", rollout_strategy)

            # One event per active serial with per-serial signals.
            for serial, s in signals["per_serial"].items():
                adapter_type = self.backend.get_adapter_type(serial)
                sample = {
                    "controller.serial": serial,
                    "bluetooth.movement_update_hz": float(s["update_hz"]),
                    "bluetooth.dropped_events_pct": float(s["dropped_pct"]),
                    "controller.adapter": adapter_type,
                }
                quality = self.connection_quality(serial)
                if quality is not None:
                    sample["bluetooth.report_gap_p95_ms"] = float(quality.p95_ms)
                    sample["bluetooth.report_gap_max_ms"] = float(quality.max_ms)
                    sample["bluetooth.connection_quality"] = quality.quality
                span.add_event("bluetooth_controller_sample", sample)

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
        record = self._records.get(serial)
        drops = errors = 0
        if record is not None:
            drops, errors = record.poll_drops, record.poll_errors
            record.poll_drops = record.poll_errors = 0
        led = self.backend.take_led_failures(serial)
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

    def _record(self, serial: str) -> _ControllerRecord:
        """The record of ``serial``, created on first use."""
        record = self._records.get(serial)
        if record is None:
            record = self._records[serial] = _ControllerRecord()
        return record

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

            adapter_type = self.backend.get_adapter_type(serial)

            # Resolve reservation metadata (None when the backend has none). Reserved
            # controllers are hidden from button-stream consumers (the menu)
            # but stay fully usable via gameplay-data streams and explicit-serial
            # LED/feedback commands (#777).
            reserved = False
            tag = ""
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
            # The poll loop runs concurrently with discovery (#1447) and may have
            # rebuilt the serial cache while this spawn awaited the first state.
            self._cached_serials = None

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
            self._record(serial).accel_gauges = {
                "magnitude": metrics.controller_accel_magnitude.labels(serial=serial),
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
            metrics.set_controller_info(serial, name)
            # Initialize poll drop/error counters so rate() always returns a series,
            # even for controllers with zero drops (needed for dashboard queries).
            metrics.controller_poll_drops_total.labels(serial=serial)
            metrics.controller_poll_errors_total.labels(serial=serial)
            # Check if this is a reconnect
            if serial in self.paired_serials:
                metrics.controller_reconnect_total.labels(serial=serial).inc()

        except Exception as e:
            logger.error(f"Error starting controller tracking {serial}: {e}", exc_info=True)


def _remove_quality_series(serial: str) -> None:
    """Drop a controller's connection-quality series (UC-035 BR-005)."""
    metrics.controller_bluetooth_report_gap_p95_seconds.remove(serial)
    metrics.controller_bluetooth_report_gap_max_seconds.remove(serial)
    metrics.controller_connection_quality.remove(serial)
