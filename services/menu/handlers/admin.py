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
from services.menu.handlers.base import ButtonDebouncer, ControllerState
from services.menu.settings_ops import (
    AGENT_LEVEL_VOICES,
    AGENT_POLICY_FLAG,
    NUM_TEAMS_VOICES,
    SENSITIVITY_VALUES,
    SENSITIVITY_VOICES,
    SettingsOps,
    agent_level_next,
    variant_to_value,
)

if TYPE_CHECKING:
    from services.menu.state_manager import StateManager

logger = logging.getLogger(__name__)

# Admin cycle option for the on-device agent kill-switch / policy control (#819).
# Selecting it and pressing SELECT/START cycles the agent's GLOBAL
# ``interventions_allowed`` policy in agent.json. This is NOT a game.json flag —
# it is the human's control over the agent (docs/OWNERSHIP_MODEL.md §6.2), so it
# is routed through a separate writer/client and handled specially in the
# value-change paths rather than the generic game.json flag cycle.
AGENT_CONTROL_OPTION = "agent_control"
# Error cue when a write fails or the flag service cannot be read (UC-013 A3, UC-012 A5/A7): short fast red pulse.
AGENT_ERROR_PULSE_MS = 400
AGENT_ERROR_PULSE_SPEED = 10
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

    async def start_game(self, serial: str, source: str, controllers: list[str] | None = None) -> None:
        """Start a coordinator game with an optional explicit controller roster."""
        ...


class AdminModeHandler:
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
        self._trigger_press_time: float | None = None
        self._trigger_hold_task: asyncio.Task | None = None

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
    def _connected_controllers(self) -> set[str]:
        """Get connected controllers from StateManager."""
        if self._state_manager is None:
            return set()
        return self._state_manager.connected_controllers

    @property
    def _ready_controllers(self) -> set[str]:
        """Get ready controllers from StateManager."""
        if self._state_manager is None:
            return set()
        return self._state_manager.ready_controllers

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
        task = asyncio.get_running_loop().create_task(self._play_voice_logged(sound))
        self._voice_tasks.add(task)
        task.add_done_callback(self._voice_tasks.discard)

    async def _play_voice_logged(self, sound: Sound) -> None:
        try:
            await self._play_voice(sound)
        except Exception as e:
            logger.warning(f"Voice line {sound} failed: {e}")

    async def _play_sound(self, sound: Sound) -> None:
        """Play sound effect via StateManager's audio helper."""
        if self._state_manager is None:
            return
        await self._state_manager.audio.play_sound(sound)

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

    def check_combo_from_controller(self, controller) -> bool:
        """
        Check if admin combo is being held based on controller object.

        Args:
            controller: Controller protobuf message with button fields

        Returns:
            True if all 4 face buttons (Cross + Circle + Square + Triangle) are pressed
        """
        return (
            controller.cross_pressed
            and controller.circle_pressed
            and controller.square_pressed
            and controller.triangle_pressed
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

        self.metrics.button_presses_total.labels(button="admin_combo", action="hold").inc()

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

    async def exit(self) -> None:
        """
        Exit admin mode and restore lobby color.

        Ends the admin_mode_session parent span.
        """
        if not self.active:
            return

        self._cancel_timeout_task()

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

        self._trigger_press_time = time.time()

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
        if self._trigger_hold_task and not self._trigger_hold_task.done():
            self._trigger_hold_task.cancel()
            self._trigger_hold_task = None
            logger.debug(f"Trigger released early, cancelled force start for {serial}")

            # Cancel the dimming effect and restore white
            await self.cancel_force_start_effect(serial)

        self._trigger_press_time = None

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

    async def handle_game_mode_change(self, serial: str, forward: bool = True) -> None:
        """
        Change game mode from admin mode.

        Only available in admin mode to prevent accidental game changes.

        Args:
            serial: Controller serial number
            forward: True to go forward through modes, False to go backward
        """
        from proto import controller_manager_pb2

        ctx = self._get_span_context()
        with self.tracer.start_as_current_span("admin_game_mode_change", context=ctx) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)
            span.set_attribute("direction", "forward" if forward else "backward")

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

                # Calculate new index
                new_idx = (current_idx + 1) % len(game_options) if forward else (current_idx - 1) % len(game_options)

                new_selection = game_options[new_idx]

                span.set_attribute("old_selection", current_selection)
                span.set_attribute("new_selection", new_selection)

                # Actually apply the selection through the menu so the coordinator
                # start path (and the dashboard) see it — not just a fire-and-forget
                # event nobody consumes (#1030). Route through the same working path
                # as handle_game_mode so a future re-wiring (e.g. a backward-cycle
                # button) can't silently reintroduce the dead-application bug.
                await self.callbacks.select_game_mode(new_selection)

                # Mirror the selection as an event for any listeners (dashboard).
                await self._publish_event(
                    "game_selection_changed",
                    {"game_name": new_selection, "source": "admin_mode", "serial": serial},
                )

                # Visual feedback: brief color pulse (green for forward, blue for backward)
                # GAME_EFFECT_PULSE restores to base color automatically
                pulse_color = (0, 255, 0) if forward else (0, 0, 255)
                await self._send_game_effect(
                    serial,
                    controller_manager_pb2.GAME_EFFECT_PULSE,
                    color=pulse_color,
                    duration_ms=400,
                    speed=6,
                )

            except Exception as e:
                logger.error(f"Error changing game mode: {e}", exc_info=True)

    async def _prune_ghosts_with_live_roster(self, controllers: list[str], admin_serial: str, span) -> list[str]:
        """Drop ghost controllers using the CM live roster (#1153); keeps the admin serial (settings_ops)."""
        return await self.settings_ops.prune_ghosts(controllers, admin_serial, span)

    async def handle_force_start(self, serial: str) -> None:
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
                await self.callbacks.start_game(serial, "admin_force_start", controllers)

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

    async def handle_sensitivity(self, serial: str) -> None:
        """
        Handle sensitivity cycling in admin mode.

        Cycles through sensitivity variants via the flag service (FlagServiceWriter).

        Args:
            serial: Controller serial number
        """
        from proto import controller_manager_pb2

        if self._state_manager is None:
            return

        ctx = self._get_span_context()
        with self.tracer.start_as_current_span("admin_sensitivity", context=ctx) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)

            try:
                writer = self._state_manager.game_settings_writer
                # The CYCLE feedback must describe the baseline transition the
                # admin is actually making: cycle_variant() advances the on-disk
                # defaultVariant (the human-owned baseline), so old_variant must
                # be the baseline value being cycled FROM, captured before the
                # mutation. Using the effective value here would announce the
                # agent's override (e.g. ultra_fast) as the source while we
                # actually write baseline medium->fast — incoherent. Selecting an
                # option and the agent-overlay follow-up after a step announce
                # the effective value from the flag-service view (_overlay_variant, #818, UC-012 BR-006).
                old_variant, new_variant = await self._cycle_baseline_guarded(writer, "sensitivity", True)

                if new_variant is None:
                    logger.warning("Failed to cycle sensitivity variant")
                    await self._signal_write_failed(serial, voice=True)
                    return

                # Map variant name to numeric value for visual feedback
                new_value = SENSITIVITY_VALUES.get(new_variant, 2)

                # Visual feedback: Color by sensitivity level (5 distinct colors)
                sensitivity_colors = [
                    Colors.Blue,  # Ultra Slow (0)
                    Colors.Turquoise,  # Slow (1)
                    Colors.Green,  # Medium (2)
                    Colors.Orange,  # Fast (3)
                    Colors.Red,  # Ultra Fast (4)
                ]
                color = sensitivity_colors[new_value].value

                # Show sensitivity color (GAME_EFFECT_PULSE restores to base automatically)
                await self._send_game_effect(
                    serial,
                    controller_manager_pb2.GAME_EFFECT_PULSE,
                    color=color,
                    duration_ms=800,
                    speed=5,
                )

                # Play voice for sensitivity level (matches original JoustMania)
                await self._play_voice(SENSITIVITY_VOICES[new_value])
                await self._announce_agent_overlay(serial, "sensitivity", new_variant)

                span.add_event(
                    "sensitivity_changed",
                    {"old_variant": old_variant, "new_variant": new_variant},
                )
                logger.info(f"Sensitivity changed by admin controller {serial}: {old_variant} -> {new_variant}")

            except Exception as e:
                logger.error(f"Error changing sensitivity: {e}", exc_info=True)

    async def handle_battery(self, serial: str) -> None:
        """
        Handle battery display in admin mode (triangle press).

        Sends GAME_EFFECT_SHOW_BATTERY to controller_manager which shows
        battery levels on ALL connected controllers using color-coded LEDs
        for 1 second, then auto-restores to previous colors.

        Color scheme (from original JoustMania):
        - 100%: Green
        - 80%: Turquoise
        - 60%: Blue
        - 40%: Yellow
        - <40%: Red

        Args:
            serial: Controller serial number
        """
        from proto import controller_manager_pb2

        ctx = self._get_span_context()
        with self.tracer.start_as_current_span("admin_battery_display", context=ctx) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)

            try:
                # Send SHOW_BATTERY effect with empty serial to affect all controllers
                if await self._send_game_effect("", controller_manager_pb2.GAME_EFFECT_SHOW_BATTERY):
                    logger.info(f"Battery display triggered by admin controller {serial}")
                    span.add_event("battery_display_triggered")
                else:
                    logger.warning("Could not trigger battery display - stream not available")

            except Exception as e:
                logger.error(f"Error triggering battery display: {e}", exc_info=True)

    async def handle_instructions(self, serial: str) -> None:
        """
        Handle instruction toggle in admin mode.

        Toggles instruction display on/off via the flag service (FlagServiceWriter).

        Args:
            serial: Controller serial number
        """
        from proto import controller_manager_pb2

        if self._state_manager is None:
            return

        ctx = self._get_span_context()
        with self.tracer.start_as_current_span("admin_instructions", context=ctx) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)

            try:
                writer = self._state_manager.user_prefs_writer
                old_variant, new_variant = await self._cycle_baseline_guarded(writer, "game_instructions", True)

                if new_variant is None:
                    logger.warning("Failed to cycle game_instructions variant")
                    await self._signal_write_failed(serial, voice=True)
                    return

                enabled = new_variant == "on"

                # Visual feedback: Green (enabled) or Red (disabled)
                # GAME_EFFECT_PULSE restores to base color automatically
                pulse_color = (0, 255, 0) if enabled else (255, 0, 0)
                await self._send_game_effect(
                    serial,
                    controller_manager_pb2.GAME_EFFECT_PULSE,
                    color=pulse_color,
                    duration_ms=800,
                    speed=5,
                )

                # Play instructions toggle voice announcement
                voice = Sound.MENU_VOX_INSTRUCTIONS_ON if enabled else Sound.MENU_VOX_INSTRUCTIONS_OFF
                await self._play_voice(voice)

                span.add_event(
                    "instructions_toggled",
                    {"old_variant": old_variant, "new_variant": new_variant, "enabled": enabled},
                )
                logger.info(f"Instructions toggled by admin controller {serial}: {old_variant} -> {new_variant}")

            except Exception as e:
                logger.error(f"Error toggling instructions: {e}", exc_info=True)

    async def handle_cycle_option(self, serial: str) -> None:
        """
        Cycle through admin options.

        Options: sensitivity -> num_teams -> force_all_start -> sensitivity

        Args:
            serial: Controller serial number
        """
        ctx = self._get_span_context()
        with self.tracer.start_as_current_span("admin_cycle_option", context=ctx) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)

            # Cycle to next option
            self.current_option = (self.current_option + 1) % len(self.option_names)
            option_name = self.option_names[self.current_option]
            option_color = self.option_colors[self.current_option]

            span.set_attribute(SpanAttr.ADMIN_OPTION, option_name)

            try:
                # Show option color for 1 second
                await self._send_base_color(serial, option_color.value)

                # Restore white after option color finishes
                async def restore_white():
                    await asyncio.sleep(1.1)
                    if self.active and serial == self.controller_serial:
                        await self._send_base_color(serial, Colors.White.value)

                # Track task to prevent garbage collection
                task = asyncio.create_task(restore_white())
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)

                span.add_event(
                    "admin_option_changed",
                    {"option": option_name, "option_index": self.current_option},
                )
                await self._announce_effective_value(serial, option_name)
                logger.info(f"Admin option changed to {option_name} by controller {serial}")

            except Exception as e:
                logger.error(f"Error cycling admin option: {e}", exc_info=True)

    async def handle_increase_value(self, serial: str) -> None:
        """
        Increase the value of the current admin option.

        Cycles through flag variants via the flag service (FlagServiceWriter).

        Args:
            serial: Controller serial number
        """
        if self._state_manager is None:
            return

        option_name = self.option_names[self.current_option]
        if option_name == AGENT_CONTROL_OPTION:
            await self.handle_agent_control(serial, forward=True)
            return

        ctx = self._get_span_context()
        with self.tracer.start_as_current_span("admin_increase_value", context=ctx) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)
            span.set_attribute(SpanAttr.ADMIN_OPTION, option_name)

            try:
                writer = self._state_manager.game_settings_writer
                # Cycle feedback describes the baseline transition (defaultVariant
                # being changed), captured before the mutation — NOT the effective
                # value, which may be an agent override (#818). The agent-overlay
                # follow-up (_announce_agent_overlay) announces the effective value.
                old_variant, new_variant = await self._cycle_baseline_guarded(writer, option_name, True)

                if new_variant is None:
                    logger.warning(f"Failed to cycle variant for {option_name}")
                    await self._signal_write_failed(serial, voice=True)
                    return

                # Get the actual value for visual/voice feedback
                new_value = self._variant_to_value(option_name, new_variant)

                # Visual feedback
                await self._show_value_feedback(serial, option_name, new_value)

                # Voice feedback
                await self._play_value_voice(option_name, new_value)
                await self._announce_agent_overlay(serial, option_name, new_variant)

                span.add_event(
                    "admin_value_increased",
                    {"option": option_name, "old_variant": str(old_variant), "new_variant": str(new_variant)},
                )
                logger.info(f"Admin increased {option_name}: {old_variant} -> {new_variant}")

            except Exception as e:
                logger.error(f"Error increasing admin value: {e}", exc_info=True)

    async def handle_decrease_value(self, serial: str) -> None:
        """
        Decrease the value of the current admin option.

        Cycles through flag variants backward via the flag service (FlagServiceWriter).

        Args:
            serial: Controller serial number
        """
        if self._state_manager is None:
            return

        option_name = self.option_names[self.current_option]
        if option_name == AGENT_CONTROL_OPTION:
            await self.handle_agent_control(serial, forward=False)
            return

        ctx = self._get_span_context()
        with self.tracer.start_as_current_span("admin_decrease_value", context=ctx) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)
            span.set_attribute(SpanAttr.ADMIN_OPTION, option_name)

            try:
                writer = self._state_manager.game_settings_writer
                # Cycle feedback describes the baseline transition (defaultVariant
                # being changed), captured before the mutation — NOT the effective
                # value, which may be an agent override (#818). The agent-overlay
                # follow-up (_announce_agent_overlay) announces the effective value.
                old_variant, new_variant = await self._cycle_baseline_guarded(writer, option_name, False)

                if new_variant is None:
                    logger.warning(f"Failed to cycle variant backward for {option_name}")
                    await self._signal_write_failed(serial, voice=True)
                    return

                # Get the actual value for visual/voice feedback
                new_value = self._variant_to_value(option_name, new_variant)

                # Visual feedback
                await self._show_value_feedback(serial, option_name, new_value)

                # Voice feedback
                await self._play_value_voice(option_name, new_value)
                await self._announce_agent_overlay(serial, option_name, new_variant)

                span.add_event(
                    "admin_value_decreased",
                    {"option": option_name, "old_variant": str(old_variant), "new_variant": str(new_variant)},
                )
                logger.info(f"Admin decreased {option_name}: {old_variant} -> {new_variant}")

            except Exception as e:
                logger.error(f"Error decreasing admin value: {e}", exc_info=True)

    async def _announce_effective_value(self, serial: str, option_name: str) -> None:
        """
        Announce the EFFECTIVE value of the selected option (colour pulse and voice line).

        The effective value is the human baseline plus any active agent delta
        (OWNERSHIP_MODEL section 6, #818; UC-012 step 3, BR-006). Options without
        a value ladder (agent_control has its own flow) are skipped. Best effort.
        """
        if self._state_manager is None or option_name not in ("sensitivity", "num_teams", "force_all_start"):
            return
        try:
            variant = await asyncio.to_thread(self._effective_variant, option_name)
            if variant is None:
                # Flag service unreadable (UC-012 A7): no value to show, so say so on the LED.
                await self._signal_write_failed(serial)
                return
            value = self._variant_to_value(option_name, variant)
            await self._show_value_feedback(serial, option_name, value)
            await self._play_value_voice(option_name, value)
        except Exception as e:
            logger.error(f"Error announcing effective value for {option_name}: {e}", exc_info=True)

    async def _announce_agent_overlay(self, serial: str, option_name: str, new_variant: str) -> None:
        """
        After a host step: when an agent overlay makes the EFFECTIVE value differ from the
        new baseline, follow the baseline announcement with the agent cue (magenta pulse,
        as the coordinator's #818 cue) and the effective value. Equal or unknown: silent.
        The overlay is read from the flag service (#2092), not the lagging in-process client.
        """
        from proto import controller_manager_pb2

        try:
            effective = await asyncio.to_thread(self._overlay_variant, option_name, new_variant)
            if effective is None:
                return
            await self._send_game_effect(
                serial,
                controller_manager_pb2.GAME_EFFECT_PULSE,
                color=Colors.Magenta.value,
                duration_ms=600,
                speed=6,
            )
            # Reinforces the magenta pulse (#1746); detached so a slow audio RPC never delays the value LED.
            self._play_voice_detached(Sound.MENU_VOX_ADMIN_AGENT)
            value = self._variant_to_value(option_name, effective)
            await self._show_value_feedback(serial, option_name, value)
            await self._play_value_voice(option_name, value)
        except Exception as e:
            logger.error(f"Error announcing agent overlay for {option_name}: {e}", exc_info=True)

    def _overlay_variant(self, option_name: str, new_variant: str) -> str | None:
        """
        The variant an overlay holds when it overrides the baseline just written, else None.

        Reads the flag service, whose answer is synchronous with the write, not the
        in-process flagd client: that one lags the write by up to a sync interval, and
        its stale value was mistaken for an agent overlay (#2092). The flag service's
        effective default is the stack overlay, else the admin value just written, so a
        difference from ``new_variant`` is a real overlay. Unreadable: None (silent).

        Blocking (one flag-service read): run via to_thread.
        """
        view = self._state_manager.game_settings_writer.get_flag_view(option_name)
        effective = view.get("effective_default") if view else None
        if effective is None or effective == new_variant:
            return None
        return effective

    @staticmethod
    def _cycle_baseline(writer, flag_key: str, forward: bool) -> tuple[str | None, str | None]:
        """(baseline variant before, variant after) one cycle step. Blocking: run via to_thread."""
        return writer.get_current_variant(flag_key), writer.cycle_variant(flag_key, forward=forward)

    def _effective_variant(self, option_name: str) -> str | None:
        """
        The EFFECTIVE variant of a game.json option, from the flag service (#818, #2092).

        The flag service's ``effective_default`` ranks the stack overlay above the admin
        value above the promotion overlay above the baseline, and it is synchronous with
        every write. The in-process flagd client is not read: it lags a write by up to a
        sync interval and would announce the previous value.

        Blocking (one flag-service read): run via to_thread.

        Returns:
            Effective variant name, or None when the flag service cannot be read (silent).
        """
        view = self._state_manager.game_settings_writer.get_flag_view(option_name)
        return view.get("effective_default") if view else None

    async def handle_agent_control(self, serial: str, forward: bool = True) -> None:
        """
        Cycle the agent intervention policy from the device (#819 kill-switch).

        Writes the GLOBAL ``interventions_allowed`` flag in agent.json through the
        menu's agent writer, cycling none -> ambient -> standard -> full (and
        back). "none" is the hard panic-off: the coordinator's allow-list then
        rejects every agent intervention, so the switch is load-bearing at the
        gate even if the agent process lags (docs/OWNERSHIP_MODEL.md §6.2).

        This is global agent policy (read with a bare context, not game-scoped),
        and it writes only agent.json — the menu/agent write surfaces stay
        disjoint, so no real-resolution invariant is touched. flagd hot-reloads
        the change within ~100ms; the agent re-reads it live per decision.

        Args:
            serial: Controller serial number
            forward: True to escalate permissiveness, False to restrict.
        """
        from proto import controller_manager_pb2

        if self._state_manager is None:
            return

        writer = getattr(self._state_manager, "agent_settings_writer", None)
        if writer is None:
            logger.warning("Agent control unavailable: no agent settings writer wired")
            return

        ctx = self._get_span_context()
        with self.tracer.start_as_current_span("admin_agent_control", context=ctx) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)
            span.set_attribute(SpanAttr.ADMIN_OPTION, AGENT_CONTROL_OPTION)

            try:
                old_level = await asyncio.to_thread(self._current_agent_level)
                if old_level is None and forward:
                    # UC-013 A5: never raise a level that cannot be read; a restrict still writes none.
                    logger.warning("Agent level unreadable: escalation refused")
                    await self._signal_agent_write_failed(serial, controller_manager_pb2)
                    return
                new_level = self._next_agent_level(old_level, forward=forward)

                try:
                    written = await asyncio.to_thread(writer.update_default_variant, AGENT_POLICY_FLAG, new_level)
                except Exception as e:
                    # Only the write itself: errors in the feedback below must not cue a failure.
                    logger.error(f"Error writing agent policy '{new_level}': {e}", exc_info=True)
                    written = False
                if not written:
                    logger.warning(f"Failed to write agent policy '{new_level}'")
                    await self._signal_agent_write_failed(serial, controller_manager_pb2, voice=True)
                    return

                span.set_attribute("old_level", old_level or "")
                span.set_attribute("new_level", new_level)

                # Visual feedback: distinct color per level so the agent's
                # permission state is unambiguous at a glance. Red = off (panic),
                # escalating warmth toward full.
                level_colors = {
                    "none": Colors.Red,  # agent disabled (panic off)
                    "ambient": Colors.Turquoise,  # cosmetic-only
                    "standard": Colors.Orange,  # tunable deltas
                    "full": Colors.Green,  # all interventions
                }
                color = level_colors.get(new_level, Colors.Purple).value
                # The pulse is cosmetic: a failure or slow stream must not drop the
                # voice line that follows (#1643).
                try:
                    await self._send_game_effect(
                        serial,
                        controller_manager_pb2.GAME_EFFECT_PULSE,
                        color=color,
                        duration_ms=800,
                        speed=5,
                    )
                except Exception as e:
                    logger.warning(f"Agent policy pulse failed for {serial}: {e}")

                # Voice feedback: "off" announces the panic-off; permissive levels
                # announce a numbered level (1..3). Reuses existing vox assets.
                voice = AGENT_LEVEL_VOICES.get(new_level)
                if voice:
                    await self._play_voice(voice)

                span.add_event(
                    "agent_policy_changed",
                    {"old_level": str(old_level), "new_level": new_level},
                )
                logger.info(f"Agent policy changed by admin {serial}: {old_level} -> {new_level}")

            except Exception as e:
                logger.error(f"Error changing agent policy: {e}", exc_info=True)

    async def _cycle_baseline_guarded(self, writer, option_name: str, forward: bool) -> tuple[str | None, str | None]:
        """_cycle_baseline in a thread; an exception from the write is logged and reported as (None, None).

        Callers treat new_variant None as a failed write and give the error cue (UC-012 A5). Only the
        write is guarded, so a voice or LED error after a successful write never cues a failure.
        """
        try:
            return await asyncio.to_thread(self._cycle_baseline, writer, option_name, forward)
        except Exception as e:
            logger.error(f"Error writing {option_name} variant: {e}", exc_info=True)
            return None, None

    async def _signal_write_failed(self, serial: str, voice: bool = False) -> None:
        """Error cue for a failed admin step write or an unreadable flag service (UC-012 A5, A7).

        The same short red pulse as the agent-level cue (UC-013 A3). ``voice`` adds the "Not saved"
        line (#1746) and is set only where a write failed, not where a read did. Best effort.
        """
        from proto import controller_manager_pb2

        await self._signal_agent_write_failed(serial, controller_manager_pb2, voice=voice)

    async def _signal_agent_write_failed(self, serial: str, controller_manager_pb2, voice: bool = False) -> None:
        """Error cue for a refused or unreachable agent-level write (UC-013 A3, #1705).

        A short, fast red pulse, so it cannot be mistaken for the "none" level (red, 800 ms, "off"
        voice); LED first, and the "Not saved" line (#1746) only reinforces it, never a level voice
        line. ``voice`` is set only where a write failed. Best effort.
        """
        try:
            await self._send_game_effect(
                serial,
                controller_manager_pb2.GAME_EFFECT_PULSE,
                color=Colors.Red.value,
                duration_ms=AGENT_ERROR_PULSE_MS,
                speed=AGENT_ERROR_PULSE_SPEED,
            )
        except Exception as e:
            logger.warning(f"Agent write error pulse failed for {serial}: {e}")
        if voice:
            try:
                await self._play_voice(Sound.MENU_VOX_ADMIN_NOT_SAVED)
            except Exception as e:
                logger.warning(f"Not-saved voice failed for {serial}: {e}")

    def _current_agent_level(self) -> str | None:
        """The agent policy level the flag service serves (UC-013 BR-003), None if unreadable. Blocking: to_thread."""
        return self.settings_ops.current_agent_level()

    # The clamped level ladder (UC-013) and the variant values are shared with the pad (settings_ops).
    _next_agent_level = staticmethod(agent_level_next)
    _variant_to_value = staticmethod(variant_to_value)

    async def _show_value_feedback(self, serial: str, option_name: str, value: int | float | bool) -> None:
        """
        Show visual feedback for admin value change.

        Args:
            serial: Controller serial number
            option_name: Name of the option that changed
            value: New value (typed)
        """
        from proto import controller_manager_pb2

        try:
            # Determine feedback color using pattern matching
            match option_name:
                case "sensitivity":
                    # 5-color gradient for sensitivity levels (0-4)
                    sensitivity_colors = [
                        Colors.Blue,  # Ultra Slow (0)
                        Colors.Turquoise,  # Slow (1)
                        Colors.Green,  # Medium (2)
                        Colors.Orange,  # Fast (3)
                        Colors.Red,  # Ultra Fast (4)
                    ]
                    pulse_color = sensitivity_colors[int(value)].value

                case "num_teams":
                    # Gradient from green (2 teams) to red (6 teams)
                    num = int(value)
                    r = int(255 * (num - 2) / 4)
                    g = int(255 * (6 - num) / 4)
                    pulse_color = (r, g, 0)

                case "force_all_start":
                    # Green for True, red for False
                    pulse_color = Colors.Green.value if value else Colors.Red.value

                case _:
                    pulse_color = Colors.White.value

            # Show feedback color (GAME_EFFECT_PULSE restores to base automatically)
            await self._send_game_effect(
                serial,
                controller_manager_pb2.GAME_EFFECT_PULSE,
                color=pulse_color,
                duration_ms=600,
                speed=6,
            )

        except Exception as e:
            logger.error(f"Error showing value feedback: {e}", exc_info=True)

    async def _play_value_voice(self, option_name: str, value: int | float | bool) -> None:
        """
        Play voice feedback for admin option value change.

        Args:
            option_name: Name of the option that changed
            value: New value (typed from _variant_to_value)
        """
        try:
            if option_name == "sensitivity":
                # Same per-level voice as Circle (A1), so Select/Start announce it too (UC-012 step 5).
                if isinstance(value, int) and 0 <= value < len(SENSITIVITY_VOICES):
                    await self._play_voice(SENSITIVITY_VOICES[value])
            elif option_name == "num_teams":
                # Play number voice (adminop_2 through adminop_6)
                voice = NUM_TEAMS_VOICES.get(value) if isinstance(value, int) else None
                if voice:
                    await self._play_voice(voice)
            elif option_name == "force_all_start":
                # Play true/false voice
                voice = Sound.MENU_VOX_ADMINOP_TRUE if value is True else Sound.MENU_VOX_ADMINOP_FALSE
                await self._play_voice(voice)
        except Exception as e:
            logger.error(f"Error playing value voice: {e}", exc_info=True)

    def reset_on_disconnect(self, serial: str) -> None:
        """
        Reset admin mode state when the admin controller disconnects.

        Args:
            serial: Serial of the disconnected controller
        """
        if self.active and serial == self.controller_serial:
            logger.info(f"Admin controller {serial} disconnected, resetting admin mode")
            self.abandon_session(span_attribute="disconnect")

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

        self.active = False
        self.controller_serial = None
        self.entry_time = 0

        if self.session_span:
            self.session_span.set_attribute(span_attribute, True)
            self.session_span.end()
            self.session_span = None
            self.session_span_context = None
