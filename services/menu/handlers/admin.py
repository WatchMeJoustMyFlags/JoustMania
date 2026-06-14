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
import os
import time
from typing import TYPE_CHECKING, Protocol

from opentelemetry import trace

from lib.colors import Colors
from lib.controller_constants import ButtonTrackingKey
from lib.telemetry import SpanAttr
from lib.types import Sound
from services.menu.handlers.base import ButtonDebouncer, ControllerState

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
AGENT_POLICY_FLAG = "interventions_allowed"

# Ordered policy levels for the kill-switch, most-restrictive first. "none" is the
# panic-off floor (agent applies no deltas — the coordinator allow-list rejects
# every intervention). Cycling forward escalates permissiveness.
AGENT_POLICY_LEVELS = ["none", "ambient", "standard", "full"]


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
    ):
        """
        Initialize the admin mode handler.

        Args:
            controller_channel: gRPC channel to Controller Manager service
            tracer: OpenTelemetry tracer for span creation
            callbacks: Menu-specific callbacks (set_menu_state, get_game_options)
            metrics: Prometheus metrics module
        """
        self.controller_channel = controller_channel
        self.tracer = tracer
        self.callbacks = callbacks
        self.metrics = metrics

        # StateManager reference (set via set_state_manager)
        self._state_manager: StateManager | None = None

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

        # Timeout task for auto-exit after 60 seconds
        self._timeout_task: asyncio.Task | None = None
        self._timeout_seconds = 60

        # Trigger hold tracking for force start
        self._trigger_press_time: float | None = None
        self._trigger_hold_task: asyncio.Task | None = None
        # Hold duration (seconds) before a trigger-hold force-starts the game.
        # Defaults to 3s for real play; CI overrides via ADMIN_FORCE_START_SECONDS
        # to a short hold so integration tests don't sleep for the full duration.
        self._force_start_threshold = float(os.environ.get("ADMIN_FORCE_START_SECONDS", "3.0"))

    # ControllerHandler protocol methods

    @property
    def state(self) -> ControllerState:
        """The state this handler manages."""
        return ControllerState.ADMIN

    def set_state_manager(self, manager: StateManager) -> None:
        """Set the state manager reference."""
        self._state_manager = manager

    async def handle_button(self, serial: str, button: str) -> None:
        """
        Handle a button press event (ControllerHandler protocol).

        Delegates to the existing handle_button_event method.

        Args:
            serial: Controller serial number
            button: Button name
        """
        await self.handle_button_event(serial, button)

    async def on_enter(self, serial: str) -> None:
        """
        Called when a controller enters admin mode (ControllerHandler protocol).

        Delegates to the existing enter method.

        Args:
            serial: Controller serial number
        """
        await self.enter(serial)

    async def on_exit(self, _serial: str) -> None:
        """
        Called when a controller exits admin mode (ControllerHandler protocol).

        Delegates to the existing exit method.

        Args:
            serial: Controller serial number (unused - uses internal state)
        """
        await self.exit()

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

    def _start_timeout_task(self, serial: str) -> None:
        """Start background task to auto-exit admin mode after timeout."""
        # Cancel existing timeout task if any
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()

        async def timeout_exit():
            try:
                await asyncio.sleep(self._timeout_seconds)
                if self.active and self.controller_serial == serial:
                    logger.info(f"Admin mode auto-exit after {self._timeout_seconds}s timeout")
                    await self._exit_to_connected(serial)
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

        # Cancel timeout task
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
            self._timeout_task = None

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

    async def handle_button_event(self, serial: str, button: str) -> None:
        """
        Handle button press in admin mode.

        Args:
            serial: Controller serial number
            button: Button name (trigger, move, cross, circle, square, triangle, ps)
        """
        # Check for admin mode timeout (60 seconds)
        if time.time() - self.entry_time > 60:
            logger.info("Admin mode timed out after 60 seconds")
            await self._exit_to_connected(serial)
            return

        if not self._debouncer.should_process(serial, button):
            return

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

        # Create task to fire force start after threshold
        async def trigger_hold_timer():
            try:
                await asyncio.sleep(self._force_start_threshold)
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

                # Publish selection change
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
                # Check force_all_start from flagd via OpenFeature
                from lib.feature_flags import get_flag_client

                gs_client = get_flag_client("game")
                force_all = gs_client.get_boolean_value("force_all_start", False)
                span.set_attribute("force_all_start", force_all)

                # Determine which controllers to include
                if force_all:
                    # Use all connected controllers
                    controllers = list(self._connected_controllers)
                else:
                    # Use ready controllers, but include admin if not already ready
                    controllers = list(self._ready_controllers)
                    if serial not in controllers:
                        controllers.append(serial)

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

        Cycles through sensitivity variants via FlagConfigWriter.

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
                # mutation. Using _effective_variant here would announce the
                # agent's override (e.g. ultra_fast) as the source while we
                # actually write baseline medium->fast — incoherent. The passive
                # display path (viewing the option) still uses _effective_variant
                # to show the truthful effective value, incl. override (#818).
                old_variant = writer.get_current_variant("sensitivity")
                new_variant = writer.cycle_variant("sensitivity", forward=True)

                if new_variant is None:
                    logger.warning("Failed to cycle sensitivity variant")
                    return

                # Map variant name to numeric value for visual feedback
                variant_to_value = {
                    "ultra_slow": 0,
                    "slow": 1,
                    "medium": 2,
                    "fast": 3,
                    "ultra_fast": 4,
                }
                new_value = variant_to_value.get(new_variant, 2)

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
                sensitivity_voices = [
                    Sound.MENU_VOX_SENSITIVITY_ULTRA_HIGH,  # ULTRA_SLOW (0)
                    Sound.MENU_VOX_SENSITIVITY_HIGH,  # SLOW (1)
                    Sound.MENU_VOX_SENSITIVITY_MEDIUM,  # MEDIUM (2)
                    Sound.MENU_VOX_SENSITIVITY_LOW,  # FAST (3)
                    Sound.MENU_VOX_SENSITIVITY_ULTRA_LOW,  # ULTRA_FAST (4)
                ]
                await self._play_voice(sensitivity_voices[new_value])

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

        Toggles instruction display on/off via FlagConfigWriter.

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
                old_variant = writer.get_current_variant("game_instructions")
                new_variant = writer.cycle_variant("game_instructions", forward=True)

                if new_variant is None:
                    logger.warning("Failed to cycle game_instructions variant")
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
                logger.info(f"Admin option changed to {option_name} by controller {serial}")

            except Exception as e:
                logger.error(f"Error cycling admin option: {e}", exc_info=True)

    async def handle_increase_value(self, serial: str) -> None:
        """
        Increase the value of the current admin option.

        Cycles through flag variants via FlagConfigWriter.

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
                # value, which may be an agent override (#818). Passive display
                # uses _effective_variant for the truthful effective value.
                old_variant = writer.get_current_variant(option_name)
                new_variant = writer.cycle_variant(option_name, forward=True)

                if new_variant is None:
                    logger.warning(f"Failed to cycle variant for {option_name}")
                    return

                # Get the actual value for visual/voice feedback
                new_value = self._variant_to_value(option_name, new_variant)

                # Visual feedback
                await self._show_value_feedback(serial, option_name, new_value)

                # Voice feedback
                await self._play_value_voice(option_name, new_value)

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

        Cycles through flag variants backward via FlagConfigWriter.

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
                # value, which may be an agent override (#818). Passive display
                # uses _effective_variant for the truthful effective value.
                old_variant = writer.get_current_variant(option_name)
                new_variant = writer.cycle_variant(option_name, forward=False)

                if new_variant is None:
                    logger.warning(f"Failed to cycle variant backward for {option_name}")
                    return

                # Get the actual value for visual/voice feedback
                new_value = self._variant_to_value(option_name, new_variant)

                # Visual feedback
                await self._show_value_feedback(serial, option_name, new_value)

                # Voice feedback
                await self._play_value_voice(option_name, new_value)

                span.add_event(
                    "admin_value_decreased",
                    {"option": option_name, "old_variant": str(old_variant), "new_variant": str(new_variant)},
                )
                logger.info(f"Admin decreased {option_name}: {old_variant} -> {new_variant}")

            except Exception as e:
                logger.error(f"Error decreasing admin value: {e}", exc_info=True)

    def _effective_variant(self, option_name: str) -> str | None:
        """
        Resolve the EFFECTIVE variant for a game.json option through flagd (#818).

        The admin display must reflect the value the game would actually resolve
        — including any agent override applied to the same flag domain — not the
        stale game.json ``defaultVariant`` on disk. We read the live value via the
        OpenFeature "game" client and map it back to a variant name.

        Falls back to the on-disk ``defaultVariant`` when no client is wired, the
        resolved value matches no known variant, or anything fails — so the
        display degrades to today's behavior rather than going blank.

        Args:
            option_name: game.json flag key

        Returns:
            Effective variant name, or the on-disk default on fallback.
        """
        writer = self._state_manager.game_settings_writer
        file_variant = writer.get_current_variant(option_name)

        # Sensitivity is the one option the agent actively contends for: its
        # global_sensitivity_override (interventions.json) overwrites the live
        # game.sensitivity in place (docs/OWNERSHIP_MODEL.md §4.1). The game.json
        # flag still reflects the human baseline, so to show the operator the
        # value the game is ACTUALLY using we consult the override first. A
        # value < 0 ("none") means no override -> fall through to the baseline.
        if option_name == "sensitivity":
            override = self._agent_sensitivity_override()
            if override is not None:
                return override

        client = getattr(self._state_manager, "game_settings_client", None)
        if client is None:
            return file_variant

        try:
            # Use the on-disk default's value as the resolution fallback so a
            # provider miss returns the file value unchanged.
            default_value = writer.get_variant_value(option_name, file_variant) if file_variant else None
            resolved = self._resolve_flag_value(client, option_name, default_value)
            if resolved is None:
                return file_variant
            matched = writer.match_variant_for_value(option_name, resolved)
            return matched or file_variant
        except Exception as e:
            logger.debug(f"Effective-variant resolution failed for '{option_name}': {e}")
            return file_variant

    def _agent_sensitivity_override(self) -> str | None:
        """Return the agent's active sensitivity override as a variant name (#818).

        Reads ``global_sensitivity_override`` from the interventions domain. The
        flag's variant names match the game.json ``sensitivity`` variant names
        1:1 (ultra_slow..ultra_fast), with ``none`` = -1 meaning "no override".
        Returns the matching sensitivity variant name when an override is active,
        else ``None`` (no override / no client / read error -> caller falls back
        to the human baseline).
        """
        client = getattr(self._state_manager, "interventions_client", None)
        if client is None:
            return None
        try:
            value = client.get_integer_value("global_sensitivity_override", -1)
            if value is None or value < 0:
                return None
            # Map the override integer back to a sensitivity variant name.
            value_to_variant = {0: "ultra_slow", 1: "slow", 2: "medium", 3: "fast", 4: "ultra_fast"}
            return value_to_variant.get(value)
        except Exception as e:
            logger.debug(f"agent sensitivity override read failed: {e}")
            return None

    @staticmethod
    def _resolve_flag_value(client, flag_key: str, default):
        """
        Resolve a flag value through an OpenFeature client, typed by ``default``.

        OpenFeature requires the correct typed getter; pick it from the default's
        Python type so booleans/ints/floats/strings/objects each resolve cleanly.
        """
        if isinstance(default, bool):
            return client.get_boolean_value(flag_key, default)
        if isinstance(default, int):
            return client.get_integer_value(flag_key, default)
        if isinstance(default, float):
            return client.get_float_value(flag_key, default)
        if isinstance(default, str):
            return client.get_string_value(flag_key, default)
        if isinstance(default, (list, dict)):
            return client.get_object_value(flag_key, default)
        # Unknown/None default: try a string read (most game.json variants are
        # scalar); a TYPE_MISMATCH returns the default, which is None here.
        return client.get_string_value(flag_key, "") or None

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
                old_level = self._current_agent_level()
                new_level = self._next_agent_level(old_level, forward=forward)

                if not writer.update_default_variant(AGENT_POLICY_FLAG, new_level):
                    logger.warning(f"Failed to write agent policy '{new_level}'")
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
                await self._send_game_effect(
                    serial,
                    controller_manager_pb2.GAME_EFFECT_PULSE,
                    color=color,
                    duration_ms=800,
                    speed=5,
                )

                # Voice feedback: "off" announces the panic-off; permissive levels
                # announce a numbered level (1..3). Reuses existing vox assets.
                level_voices = {
                    "none": Sound.MENU_VOX_ADMINOP_OFF,
                    "ambient": Sound.MENU_VOX_ADMINOP_1,
                    "standard": Sound.MENU_VOX_ADMINOP_2,
                    "full": Sound.MENU_VOX_ADMINOP_3,
                }
                voice = level_voices.get(new_level)
                if voice:
                    await self._play_voice(voice)

                span.add_event(
                    "agent_policy_changed",
                    {"old_level": str(old_level), "new_level": new_level},
                )
                logger.info(f"Agent policy changed by admin {serial}: {old_level} -> {new_level}")

            except Exception as e:
                logger.error(f"Error changing agent policy: {e}", exc_info=True)

    def _current_agent_level(self) -> str | None:
        """Read the current effective agent policy level (#819).

        Prefers the live flagd value (agent-domain client) so the cycle advances
        from what the agent is actually using; falls back to the on-disk
        defaultVariant.
        """
        writer = self._state_manager.agent_settings_writer
        file_level = writer.get_current_variant(AGENT_POLICY_FLAG)

        client = getattr(self._state_manager, "agent_settings_client", None)
        if client is None:
            return file_level
        try:
            default_value = writer.get_variant_value(AGENT_POLICY_FLAG, file_level) if file_level else []
            resolved = client.get_object_value(AGENT_POLICY_FLAG, default_value)
            matched = writer.match_variant_for_value(AGENT_POLICY_FLAG, resolved)
            return matched or file_level
        except Exception as e:
            logger.debug(f"Agent level resolution failed: {e}")
            return file_level

    @staticmethod
    def _next_agent_level(current: str | None, forward: bool = True) -> str:
        """Return the next policy level, cycling AGENT_POLICY_LEVELS."""
        levels = AGENT_POLICY_LEVELS
        if current not in levels:
            # Unknown/missing current value: forward starts permissive ladder at
            # the first level; backward jumps to panic-off. This is also the
            # benign fail-safe for non-ladder variants — e.g. agent.json's
            # `probe` variant of interventions_allowed isn't in AGENT_POLICY_LEVELS,
            # so the next press collapses it to the ladder start (none).
            return levels[0] if forward else "none"
        idx = levels.index(current)
        new_idx = (idx + 1) % len(levels) if forward else (idx - 1) % len(levels)
        return levels[new_idx]

    @staticmethod
    def _variant_to_value(option_name: str, variant_name: str) -> int | float | bool:
        """
        Convert a flag variant name to a typed value for display feedback.

        Args:
            option_name: Flag key name
            variant_name: Variant name from the flag file

        Returns:
            Typed value corresponding to the variant
        """
        # Map variant names to their values for feedback purposes
        variant_maps = {
            "sensitivity": {"ultra_slow": 0, "slow": 1, "medium": 2, "fast": 3, "ultra_fast": 4},
            "num_teams": {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6},
            "random_assignment": {"on": True, "off": False},
            "nonstop.time_limit_seconds": {
                "unlimited": 0,
                "one_min": 60,
                "two_min": 120,
                "three_min": 180,
                "four_min": 240,
                "five_min": 300,
            },
            "invincibility_seconds": {
                "2s": 2.0,
                "3s": 3.0,
                "4s": 4.0,
                "5s": 5.0,
                "6s": 6.0,
                "7s": 7.0,
                "8s": 8.0,
            },
            "fight_club.min_rounds": {"five": 5, "ten": 10, "fifteen": 15, "twenty": 20},
            "werewolf.reveal_time_seconds": {
                "20s": 20.0,
                "25s": 25.0,
                "30s": 30.0,
                "35s": 35.0,
                "40s": 40.0,
                "45s": 45.0,
                "50s": 50.0,
                "55s": 55.0,
                "60s": 60.0,
            },
            "force_all_start": {"on": True, "off": False},
        }
        mapping = variant_maps.get(option_name, {})
        return mapping.get(variant_name, 0)

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

                case "random_assignment" | "force_all_start":
                    # Green for True, red for False
                    pulse_color = Colors.Green.value if value else Colors.Red.value

                case "nonstop.time_limit_seconds":
                    # Purple intensity based on time (0=dim, 300=bright)
                    intensity = int(255 * int(value) / 300) if int(value) > 0 else 50
                    pulse_color = (intensity, 0, intensity)

                case "invincibility_seconds":
                    # Green intensity based on duration (2s=dim, 8s=bright)
                    intensity = int(255 * (float(value) - 2.0) / 6.0)
                    pulse_color = (0, intensity + 50, 0)

                case "fight_club.min_rounds":
                    # Orange intensity based on rounds (5=dim, 20=bright)
                    intensity = int(255 * (int(value) - 5) / 15)
                    pulse_color = (255, intensity + 50, 0)

                case "werewolf.reveal_time_seconds":
                    # Purple intensity based on time (20s=dim, 60s=bright)
                    intensity = int(255 * (float(value) - 20.0) / 40.0)
                    pulse_color = (intensity + 50, 0, 255)

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
            if option_name == "num_teams":
                # Play number voice (adminop_2 through adminop_6)
                num_teams_voices = {
                    "2": Sound.MENU_VOX_ADMINOP_2,
                    "3": Sound.MENU_VOX_ADMINOP_3,
                    "4": Sound.MENU_VOX_ADMINOP_4,
                    "5": Sound.MENU_VOX_ADMINOP_5,
                    "6": Sound.MENU_VOX_ADMINOP_6,
                }
                voice = num_teams_voices.get(str(value))
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

            # Cancel timeout task
            if self._timeout_task and not self._timeout_task.done():
                self._timeout_task.cancel()
                self._timeout_task = None

            self.active = False
            self.controller_serial = None
            self.entry_time = 0

            # End span if active
            if self.session_span:
                self.session_span.set_attribute("disconnect", True)
                self.session_span.end()
                self.session_span = None
                self.session_span_context = None
