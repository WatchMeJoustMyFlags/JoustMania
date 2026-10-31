"""
Admin mode handler for Menu service.

Manages the admin mode state, commands, and visual feedback when a controller
holds all 4 face buttons (Cross + Circle + Square + Triangle) simultaneously.

Button mappings in admin mode:
- Cross: Cycle game mode (flash color + voice)
- Circle: Cycle sensitivity (unchanged)
- Square: Toggle instructions (unchanged)
- Triangle: Show battery (unchanged)
- Move: Cycle between options (sensitivity / num_teams / force_all_start /
  agent_control). agent_control (#819) cycles the agent kill-switch policy.
- Trigger: Hold 3s = Force start game (LED dims during hold)
- Select: Increase current option value (agent_control: escalate agent policy)
- Start: Decrease current option value (agent_control: restrict agent policy)
- PS: Exit admin mode (unchanged)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Protocol

from opentelemetry import trace

from lib.colors import Colors
from lib.controller_constants import ButtonTrackingKey
from lib.feature_flags import read_float_flag
from lib.telemetry import SpanAttr
from lib.types import Sound
from services.menu.handlers.admin_options import (
    AGENT_CONTROL_OPTION,
    AdminOptionsMixin,
)
from services.menu.handlers.base import ButtonDebouncer, ControllerState
from services.menu.settings_ops import (
    SettingsOps,
)
from services.menu.utils.detached import run_detached

if TYPE_CHECKING:
    from services.menu.state_manager import StateManager

logger = logging.getLogger(__name__)

# The agent key (UC-013 BR-001), the level ladder (clamped, never wrapping) and the voice
# tables live in services/menu/settings_ops.py, shared with the operator pad commands (#2050).


class AdminModeCallbacks(Protocol):
    """Protocol defining Menu-specific callbacks that AdminModeHandler needs."""

    def set_menu_state(self, state) -> None:
        """Set the menu state (for game starting)."""
        ...

    def get_game_options(self) -> list[str]:
        """Get list of available game options."""
        ...

    async def select_game_mode(self, game_name: str) -> None:
        """Select a game mode by name (updates menu selection + persists)."""
        ...

    def get_menu_state(self) -> int:
        """Get the current menu state."""
        ...

    async def start_game(self, serial: str, source: str, controllers: list[str] | None = None) -> bool:
        """Start a coordinator game with an optional explicit roster; False when the start is refused."""
        ...


class AdminModeHandler(AdminOptionsMixin):
    """
    Handles admin mode state and commands.

    Admin mode is activated by pressing all 4 face buttons simultaneously
    (Cross + Circle + Square + Triangle). While active, buttons perform
    administrative actions.

    Implements the ControllerHandler protocol for StateManager integration.
    """

    def __init__(
        self,
        controller_channel,
        tracer: trace.Tracer,
        callbacks: AdminModeCallbacks,
        metrics,
        settings_ops: SettingsOps | None = None,
    ):
        """
        Initialize the admin mode handler.

        Args:
            controller_channel: gRPC channel to Controller Manager service
            tracer: OpenTelemetry tracer for span creation
            callbacks: Menu-specific callbacks (set_menu_state, get_game_options)
            metrics: Prometheus metrics module
            settings_ops: the menu's shared settings core (#2050); None builds one for this handler
        """
        self.controller_channel = controller_channel
        self.tracer = tracer
        self.callbacks = callbacks
        self.metrics = metrics

        # StateManager reference (set via set_state_manager)
        self._state_manager: StateManager | None = None

        # Roster, ghost pruning, agent level reads and value tables shared with the pad commands.
        self.settings_ops = settings_ops or SettingsOps(
            get_state_manager=lambda: self._state_manager,
            controller_channel=controller_channel,
            metrics=metrics,
            callbacks=callbacks,
        )

        # Detached voice lines in flight (strong refs so they are not collected mid-play)
        self._voice_tasks: set[asyncio.Task] = set()

        # Admin mode state
        self.active = False
        self.controller_serial: str | None = None
        self.entry_time: float = 0
        self.combo_shown = False

        # Tracing state
        self.session_span: trace.Span | None = None
        self.session_span_context = None

        # Admin option navigation
        # The controller-cycled surface is intentionally limited to the human
        # essentials (issue #815): a baseline calibration knob (sensitivity), a
        # structural pre-game choice (num_teams), and a live force-start toggle
        # (force_all_start). The remaining per-mode tunables
        # (random_assignment, nonstop.time_limit_seconds, invincibility_seconds,
        # fight_club.min_rounds, werewolf.reveal_time_seconds) still EXIST as
        # game.json flags and stay settable via dashboard/file/flagd — they are
        # simply no longer cycled blind on the controller. See
        # docs/OWNERSHIP_MODEL.md §3 for the per-flag keep/move decision.
        self.current_option = 0
        # UC-012 BR-002: the controller-cycled options; the other game.json tunables are file / flagd only.
        self.option_names = [
            "sensitivity",  # 0-4 (Ultra Slow to Ultra Fast)
            "num_teams",  # 2-6 teams (for Teams, RandomTeams, Traitor)
            "force_all_start",  # True/False (live-evaluated at force start)
            AGENT_CONTROL_OPTION,  # agent kill-switch / policy (#819)
        ]
        self.option_colors = [
            Colors.Blue,  # sensitivity
            Colors.Turquoise,  # num_teams
            Colors.Orange,  # force_all_start
            Colors.Purple,  # agent_control
        ]

        # Button debouncing (300ms for admin mode)
        self._debouncer = ButtonDebouncer(default_interval=0.3)

        # Background tasks (to prevent garbage collection)
        self._pending_tasks: set[asyncio.Task] = set()

        # UC-012 A4 / NFR-006: admin mode exits 60 s after entry (the one timeout source).
        self._timeout_task: asyncio.Task | None = None
        self._timeout_seconds = 60

        # Trigger hold tracking for force start
        self._trigger_hold_task: asyncio.Task | None = None
        # True while handle_force_start runs past its hold: exit/abandon from another path must not kill it.
        self._force_starting = False

    # ControllerHandler protocol methods

    @property
    def state(self) -> ControllerState:
        """The state this handler manages."""
        return ControllerState.ADMIN

    def set_state_manager(self, manager: StateManager) -> None:
        """Set the state manager reference."""
        self._state_manager = manager

    async def handle_button(self, serial: str, button: str, event_time: float | None = None) -> None:
        """
        Handle a button press event (ControllerHandler protocol).

        Delegates to the existing handle_button_event method.

        Args:
            serial: Controller serial number
            button: Button name
        """
        await self.handle_button_event(serial, button, event_time)

    async def on_enter(self, serial: str) -> None:
        """
        Called when a controller enters admin mode (ControllerHandler protocol).

        Delegates to the existing enter method.

        Args:
            serial: Controller serial number
        """
        await self.enter(serial)

    async def on_exit(self, serial: str) -> None:
        """
        Called when a controller exits admin mode (ControllerHandler protocol).

        Delegates to the existing exit method, but only for the controller that
        owns the admin session — another controller leaving ADMIN must never end
        (or send the exit effect for) the active admin's session.

        Args:
            serial: Controller serial number
        """
        if self._owned_by_other(serial):
            logger.warning(f"Ignoring admin on_exit for {serial}: session owned by {self.controller_serial}")
            return
        await self.exit()

    def _owned_by_other(self, serial: str) -> bool:
        """True if an admin session is recorded for a controller other than ``serial``."""
        return self.active and self.controller_serial is not None and self.controller_serial != serial

    def _session_is_stale(self) -> bool:
        """True if a session is recorded but its owner is no longer in ADMIN state.

        Lobby resets, stream reconnects and StopMenu clear controller state
        without running on_exit; such a session must not keep the admin lock.
        """
        if not self.active or self._state_manager is None or self.controller_serial is None:
            return False
        return self._state_manager.get_controller_state(self.controller_serial) != ControllerState.ADMIN

    def is_held_by_other(self, serial: str) -> bool:
        """True if admin mode is actively held by a controller other than ``serial``.

        UC-012 BR-004: only one controller may be in admin mode at a time.
        A stale session (owner no longer in ADMIN state) does not hold the lock.
        """
        return self._owned_by_other(serial) and not self._session_is_stale()

    def release_stale_session(self) -> None:
        """Abandon the recorded session if its owner is no longer in ADMIN state."""
        if self._session_is_stale():
            logger.warning(f"Releasing stale admin session of {self.controller_serial}")
            self.abandon_session()

    # Helper properties for StateManager access

    @property
    def _current_game_mode(self) -> str:
        """Get current game mode name from StateManager."""
        if self._state_manager is None:
            return "JoustFFA"
        return self._state_manager.current_game_mode.name

    async def _send_game_effect(
        self,
        serial: str,
        effect: int,
        color: tuple[int, int, int] | None = None,
        duration_ms: int = 0,
        speed: int = 0,
    ) -> bool:
        """Send game effect via StateManager's LED controller."""
        if self._state_manager is None:
            return False
        return await self._state_manager.led.send_game_effect(
            serial, effect, color=color, duration_ms=duration_ms, speed=speed
        )

    async def _send_base_color(self, serial: str, color: tuple[int, int, int]) -> bool:
        """Send base color via StateManager's LED controller."""
        if self._state_manager is None:
            return False
        return await self._state_manager.led.send_base_color(serial, color)

    async def _publish_event(self, event_type: str, data: dict) -> None:
        """Publish event via StateManager."""
        if self._state_manager is None:
            return
        await self._state_manager.publish_event(event_type, data)

    async def _play_voice(self, sound: Sound) -> None:
        """Play voice via StateManager's audio helper."""
        if self._state_manager is None:
            return
        await self._state_manager.audio.play_voice(sound)

    def _play_voice_detached(self, sound: Sound) -> None:
        """Play a voice line without awaiting it (it may overlap later feedback); errors are logged."""
        run_detached(self._play_voice(sound), tasks=self._voice_tasks, what=f"Voice line {sound}")

    # Original properties

    @property
    def is_active(self) -> bool:
        """Check if admin mode is currently active."""
        return self.active

    @property
    def admin_controller(self) -> str | None:
        """Get the serial of the controller in admin mode."""
        return self.controller_serial

    def is_admin_controller(self, serial: str) -> bool:
        """Check if the given serial is the admin controller."""
        return self.active and serial == self.controller_serial

    def check_combo_from_state(self, button_state: dict[str, bool]) -> bool:
        """
        Check if admin combo is being held based on button state dict.

        Args:
            button_state: Dictionary with button names as keys and pressed state as values

        Returns:
            True if all 4 face buttons (Cross + Circle + Square + Triangle) are pressed
        """
        return (
            button_state.get("cross", False)
            and button_state.get("circle", False)
            and button_state.get("square", False)
            and button_state.get("triangle", False)
        )

    def _get_span_context(self):
        """Get the admin mode span context for child spans."""
        return self.session_span_context if self.session_span_context else None

    async def enter(self, serial: str) -> None:
        """
        Enter admin mode.

        Creates a parent admin_mode_session span that encompasses all admin actions.
        The span is ended when exit() is called.

        Args:
            serial: Controller serial number
        """
        from proto import controller_manager_pb2

        # Create parent span for entire admin mode session (ended in exit)
        self.session_span = self.tracer.start_span("admin_mode_session")
        self.session_span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)
        self.session_span_context = trace.set_span_in_context(self.session_span)

        # Create child span for the entry operation
        with self.tracer.start_as_current_span("enter_admin_mode", context=self.session_span_context) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)

            self.active = True
            self.controller_serial = serial
            self.entry_time = time.time()
            self.current_option = 0  # Reset to first option (team_size)

            # Send admin enter effect via stream
            if await self._send_game_effect(serial, controller_manager_pb2.GAME_EFFECT_ADMIN_ENTER):
                logger.info(f"Admin mode entered by controller {serial}")
                span.add_event("admin_mode_entered")
            else:
                logger.warning(f"Failed to signal admin mode entry for {serial} - stream not available")

            # Start timeout task to auto-exit after 60 seconds
            self._start_timeout_task(serial)

    def _cancel_timeout_task(self) -> None:
        """Cancel the auto-exit timeout, unless it is the task running this code.

        The timeout itself exits via exit(); cancelling the current task there
        would abort exit() at its next await, leaving the session active.
        """
        task = self._timeout_task
        self._timeout_task = None
        if task is None or task.done():
            return
        try:
            current = asyncio.current_task()
        except RuntimeError:  # no running event loop (sync caller)
            current = None
        if task is not current:
            task.cancel()

    def _start_timeout_task(self, serial: str) -> None:
        """Start background task to auto-exit admin mode after timeout."""
        self._cancel_timeout_task()

        async def timeout_exit():
            try:
                await asyncio.sleep(self._timeout_seconds)
                if self.active and self.controller_serial == serial:
                    logger.info(f"Admin mode auto-exit after {self._timeout_seconds}s timeout")
                    await self._exit_to_connected(serial)
                    # transition_to is a no-op if the controller is no longer
                    # ADMIN (state cleared elsewhere): end the session directly.
                    if self.active and self.controller_serial == serial:
                        await self.exit()
            except asyncio.CancelledError:  # NOSONAR — intentional: timeout task cancelled on early exit
                pass

        self._timeout_task = asyncio.create_task(timeout_exit())

    def _cancel_trigger_hold_task(self) -> None:
        """Cancel the force-start hold timer, unless it is the task running this code.

        The timer reaches exit() through handle_force_start; cancelling itself there
        would abort the start at the next await. A timer that outlives the session
        would force-start a game after a quick leave and re-entry (#2377).
        """
        task = self._trigger_hold_task
        self._trigger_hold_task = None
        if task is None or task.done() or self._force_starting:
            return
        try:
            current = asyncio.current_task()
        except RuntimeError:  # no running event loop (sync caller)
            current = None
        if task is not current:
            task.cancel()

    async def exit(self) -> None:
        """
        Exit admin mode and restore lobby color.

        Ends the admin_mode_session parent span.
        """
        if not self.active:
            return

        self._cancel_timeout_task()
        self._cancel_trigger_hold_task()

        from proto import controller_manager_pb2

        duration = time.time() - self.entry_time

        # Create child span for exit operation (under admin_mode_session)
        with self.tracer.start_as_current_span("exit_admin_mode", context=self.session_span_context) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, self.controller_serial)
            span.set_attribute("duration_seconds", duration)

            logger.info(f"Admin mode exited by controller {self.controller_serial}")

            # Send exit effect via stream
            serial = self.controller_serial
            if await self._send_game_effect(serial, controller_manager_pb2.GAME_EFFECT_ADMIN_EXIT):
                logger.debug(f"Admin exit effect sent via stream for {serial}")
            else:
                logger.warning(f"Failed to send admin exit effect for {serial} - stream not available")

            # Clear admin mode state
            self.active = False
            self.controller_serial = None
            self.entry_time = 0
            span.add_event("admin_mode_exited")

        # End the parent admin_mode_session span
        if self.session_span:
            self.session_span.set_attribute("session.duration_seconds", duration)
            self.session_span.end()
            self.session_span = None
            self.session_span_context = None

    async def handle_button_event(self, serial: str, button: str, event_time: float | None = None) -> None:
        """
        Handle button press in admin mode.

        Args:
            serial: Controller serial number
            button: Button name (trigger, move, cross, circle, square, triangle, ps)
            event_time: When the press happened (seconds), debounced on instead of processing time (BR-007)
        """
        # UC-012 BR-004: only the controller that owns the admin session may issue admin commands.
        if self.is_held_by_other(serial):
            logger.warning(f"Ignoring admin button {button} from {serial}: session owned by {self.controller_serial}")
            return

        # UC-012 A4 / NFR-006: a press after the timeout exits, in case the timer task has not run yet.
        # Same limit as the timer task (_timeout_seconds), so NFR-006 has one source.
        if time.time() - self.entry_time > self._timeout_seconds:
            logger.info(f"Admin mode timed out after {self._timeout_seconds} seconds")
            await self._exit_to_connected(serial)
            return

        if not self._debouncer.should_process(serial, button, event_time=event_time):
            return

        # UC-012 BR-005: the admin button map.
        match button:
            case ButtonTrackingKey.MOVE:
                await self.handle_cycle_option(serial)
            case ButtonTrackingKey.TRIGGER:
                await self._start_trigger_hold_tracking(serial)
            case ButtonTrackingKey.SELECT:
                await self.handle_increase_value(serial)
            case ButtonTrackingKey.START:
                await self.handle_decrease_value(serial)
            case ButtonTrackingKey.CROSS:
                await self.handle_game_mode(serial)
            case ButtonTrackingKey.CIRCLE:
                await self.handle_sensitivity(serial)
            case ButtonTrackingKey.TRIANGLE:
                await self.handle_battery(serial)
            case ButtonTrackingKey.SQUARE:
                await self.handle_instructions(serial)
            case ButtonTrackingKey.PS:
                await self._exit_to_connected(serial)

    async def _exit_to_connected(self, serial: str) -> None:
        """
        Exit admin mode and transition back to CONNECTED state.

        Uses StateManager to properly transition states.

        Args:
            serial: Controller serial number
        """
        if self._state_manager is not None:
            await self._state_manager.transition_to(serial, ControllerState.CONNECTED)
        else:
            # Fallback if StateManager not set
            await self.exit()

    def _force_start_threshold(self) -> float:
        """Hold duration (seconds) before a trigger-hold force-starts the game.

        Read at USE TIME, not cached at ``__init__`` — by the time a user holds
        the trigger to force-start, flagd is up and resolves correctly. A read at
        startup would fail-open before flagd is reachable (#1177) and never pick
        up the CI override.

        Defaults to 3s for real play; CI overrides via the ``force_all_start_seconds``
        game flag to a short hold so integration tests don't sleep for the full
        duration (#1215). Read with the typed float getter; fail-open to 3.0 if
        flagd is unreachable/undefined (#1177) or hits the #1127 TYPE_MISMATCH trap.
        """
        return read_float_flag("game", "force_all_start_seconds", 3.0)

    async def _start_trigger_hold_tracking(self, serial: str) -> None:
        """
        Start tracking trigger hold for force start.

        Begins 3-second timer with LED dim effect. If trigger is held
        for full duration, triggers force start.

        Args:
            serial: Controller serial number
        """
        # Cancel any existing hold task
        if self._trigger_hold_task and not self._trigger_hold_task.done():
            self._trigger_hold_task.cancel()

        # Start the dimming effect
        await self.start_force_start_effect(serial)

        # Read the hold threshold at use time (flagd is up once a user holds the
        # trigger), not cached at __init__ where it would fail-open before flagd
        # is reachable (#1177).
        threshold = self._force_start_threshold()

        # Create task to fire force start after threshold
        async def trigger_hold_timer():
            try:
                await asyncio.sleep(threshold)
                # If we get here, trigger was held long enough
                if self.active and serial == self.controller_serial:
                    logger.info(f"Trigger hold completed, triggering force start for {serial}")
                    await self.handle_force_start(serial)
            except asyncio.CancelledError:  # NOSONAR — intentional: hold-timer cancelled on trigger release
                pass

        self._trigger_hold_task = asyncio.create_task(trigger_hold_timer())
        self._pending_tasks.add(self._trigger_hold_task)
        self._trigger_hold_task.add_done_callback(self._pending_tasks.discard)

    async def handle_trigger_release(self, serial: str) -> None:
        """
        Handle trigger release in admin mode.

        Cancels force start if trigger released before 3 seconds.

        Args:
            serial: Controller serial number
        """
        if not self.is_admin_controller(serial):
            return

        # Cancel hold task if running
        # A hold that already fired is running handle_force_start; releasing must not abort it (#2377)
        if self._trigger_hold_task and not self._trigger_hold_task.done() and not self._force_starting:
            self._trigger_hold_task.cancel()
            self._trigger_hold_task = None
            logger.debug(f"Trigger released early, cancelled force start for {serial}")

            # Cancel the dimming effect and restore white
            await self.cancel_force_start_effect(serial)

    async def handle_game_mode(self, serial: str) -> None:
        """
        Handle game mode cycling (Cross button).

        Cycles through game modes forward, shows game color flash and plays voice.

        Args:
            serial: Controller serial number
        """
        from proto import controller_manager_pb2
        from services.menu.utils.led import GAME_MODE_COLORS

        ctx = self._get_span_context()
        with self.tracer.start_as_current_span("admin_game_mode", context=ctx) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)

            try:
                game_options = self.callbacks.get_game_options()
                current_selection = self._current_game_mode

                if not game_options:
                    logger.warning("No game options available for mode change")
                    return

                # Find current index
                try:
                    current_idx = game_options.index(current_selection)
                except ValueError:
                    current_idx = 0

                # Cycle forward
                new_idx = (current_idx + 1) % len(game_options)
                new_selection = game_options[new_idx]

                span.set_attribute("old_selection", current_selection)
                span.set_attribute("new_selection", new_selection)

                # Actually apply the selection through the menu so the coordinator
                # start path (and the dashboard) see it — not just a fire-and-
                # forget event nobody consumes (#1030). select_game_mode updates
                # current_selection, the state_manager game mode, and persists.
                await self.callbacks.select_game_mode(new_selection)

                # Mirror the selection as an event for any listeners (dashboard).
                await self._publish_event(
                    "game_selection_changed",
                    {"game_name": new_selection, "source": "admin_mode", "serial": serial},
                )

                # Visual feedback: flash game mode color
                game_color = GAME_MODE_COLORS.get(new_selection, (255, 165, 0))  # Default orange
                await self._send_game_effect(
                    serial,
                    controller_manager_pb2.GAME_EFFECT_PULSE,
                    color=game_color,
                    duration_ms=600,
                    speed=6,
                )

                # Play game mode voice announcement
                if self._state_manager is not None:
                    await self._state_manager.audio.play_game_mode_voice(new_selection)

                span.add_event(
                    "game_mode_changed",
                    {"old_selection": current_selection, "new_selection": new_selection},
                )
                logger.info(f"Game mode changed by admin {serial}: {current_selection} -> {new_selection}")

            except Exception as e:
                logger.error(f"Error changing game mode: {e}", exc_info=True)

    async def handle_force_start(self, serial: str) -> None:
        """Force start the game; a start already past its hold is not cancelled by exit/abandon (#2377)."""
        self._force_starting = True
        try:
            await self._handle_force_start_impl(serial)
        finally:
            self._force_starting = False

    def _restore_menu_state_if_starting(self, previous_state: int) -> None:
        """Undo our GAME_STARTING, but only while it is still the state (a newer state stays)."""
        from proto import menu_pb2

        if self.callbacks.get_menu_state() == menu_pb2.MenuState.GAME_STARTING:
            self.callbacks.set_menu_state(previous_state)

    async def _handle_force_start_impl(self, serial: str) -> None:
        """
        Force start the game from admin mode.

        Triggered when admin holds trigger for 2 seconds.
        Starts the game with currently ready controllers (or all if force_all_start=true).

        Args:
            serial: Controller serial number
        """
        from proto import controller_manager_pb2

        if self._state_manager is None:
            return

        ctx = self._get_span_context()
        with self.tracer.start_as_current_span("admin_force_start", context=ctx) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)
            span.set_attribute("game.name", self._current_game_mode)

            try:
                # UC-012 BR-001: force_all_start is read at force start, not when it was set.
                # All connected with force_all_start, else the ready ones plus this admin; ghosts
                # pruned against the controller manager's live roster (#1153/#551), fail-soft.
                controllers = await self.settings_ops.force_start_roster(serial, span)

                span.set_attribute("controller.count", len(controllers))

                # Visual feedback: White flash before starting
                await self._send_game_effect(
                    serial,
                    controller_manager_pb2.GAME_EFFECT_FLASH,
                    color=(255, 255, 255),
                    duration_ms=500,
                    speed=10,
                )

                # Record span event before exit() ends the parent session span
                span.add_event(
                    "force_start_triggered",
                    {
                        "game": self._current_game_mode,
                        "player_count": len(controllers),
                    },
                )

                # Capture the selected mode before exit() tears down session state.
                game_name = self._current_game_mode

                # Exit admin mode (ends session_span)
                await self.exit()

                # Small delay for visual feedback
                await asyncio.sleep(0.3)

                # Mark the menu as starting and announce the request (consumed by
                # the dashboard / integration tests).
                from proto import menu_pb2

                previous_state = self.callbacks.get_menu_state()
                self.callbacks.set_menu_state(menu_pb2.MenuState.GAME_STARTING)
                await self._publish_event(
                    "game_requested",
                    {
                        "game_name": game_name,
                        "source": "admin_force_start",
                        "serial": serial,
                        "controllers": json.dumps(controllers),
                    },
                )

                # Actually launch the game through the coordinator start path
                # (#1029) with the force_all_start-aware roster — previously this
                # only lit up the menu but no game began. _start_game resets the
                # menu state if the roster is too small, so we don't get stuck.
                started = False
                try:
                    started = await self.callbacks.start_game(serial, "admin_force_start", controllers)
                finally:
                    if not started:
                        # Refused or raised (a game lifecycle is still live, e.g. the lobby restore):
                        # GAME_STARTING would drop every button event until reset_menu (#2377).
                        self._restore_menu_state_if_starting(previous_state)
                if not started:
                    logger.warning(f"Force start from {serial} refused, menu state restored")
                    return

                logger.info(
                    f"Force starting game '{game_name}' via admin controller {serial} with {len(controllers)} players"
                )

            except Exception as e:
                logger.error(f"Error force starting game: {e}", exc_info=True)

    async def start_force_start_effect(self, serial: str) -> None:
        """
        Start the fade-out effect for force start countdown.

        Sends GAME_EFFECT_FORCE_START_CHARGE via bidirectional stream.
        LED fades white to dim over 2s.

        Args:
            serial: Controller serial number
        """
        from proto import controller_manager_pb2

        try:
            if await self._send_game_effect(serial, controller_manager_pb2.GAME_EFFECT_FORCE_START_CHARGE):
                logger.debug(f"Started force start fade effect for {serial}")
            else:
                logger.warning(f"Could not start force start effect for {serial} - stream not available")
        except Exception as e:
            logger.error(f"Error starting force start effect: {e}")

    async def cancel_force_start_effect(self, serial: str) -> None:
        """
        Cancel the force start effect and restore admin mode white color.

        Sends base color via bidirectional stream to cancel effect and restore white.

        Args:
            serial: Controller serial number
        """
        try:
            if await self._send_base_color(serial, (255, 255, 255)):
                logger.debug(f"Cancelled force start effect for {serial}")
            else:
                logger.warning(f"Could not cancel force start effect for {serial} - stream not available")
        except Exception as e:
            logger.error(f"Error cancelling force start effect: {e}")

    def abandon_session(self, span_attribute: str = "abandoned") -> None:
        """
        Drop the admin session without the exit effect or a state transition.

        Used when controller state is cleared wholesale (lobby reset, stream
        reconnect, StopMenu) so the single-admin lock (UC-012 BR-004) is released.

        Args:
            span_attribute: Boolean attribute set on the session span to record why.
        """
        if not self.active:
            return

        self._cancel_timeout_task()
        self._cancel_trigger_hold_task()

        self.active = False
        self.controller_serial = None
        self.entry_time = 0

        if self.session_span:
            self.session_span.set_attribute(span_attribute, True)
            self.session_span.end()
            self.session_span = None
            self.session_span_context = None
