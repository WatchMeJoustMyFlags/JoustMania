"""Controller state manager for the Menu service."""

import asyncio
import logging
from collections.abc import Callable, Coroutine

from lib.controller_constants import ButtonTrackingKey
from lib.flag_service_writer import FlagServiceWriter
from lib.types import Games
from services.menu import metrics
from services.menu.handlers.base import ControllerHandler, ControllerState
from services.menu.utils import AudioHelper, LedController

logger = logging.getLogger(__name__)


class StateManager:
    """
    Central coordinator for controller states in the menu.

    Tracks which state each controller is in and dispatches button events
    to the appropriate handler. Manages state transitions with proper
    callbacks to handlers.
    """

    def __init__(
        self,
        led: LedController,
        audio: AudioHelper,
        game_settings_writer: FlagServiceWriter,
        user_prefs_writer: FlagServiceWriter,
        publish_event: Callable[[str, dict], Coroutine],
        *,
        agent_settings_writer: FlagServiceWriter | None = None,
        game_settings_client=None,
        agent_settings_client=None,
        select_game_mode: Callable[..., Coroutine] | None = None,
    ):
        """
        Initialize state manager.

        Args:
            led: LED controller utility
            audio: Audio helper utility
            game_settings_writer: flag-service writer for the game set
            user_prefs_writer: flag-service writer for the user set
            publish_event: Async function to publish events
            agent_settings_writer: flag-service writer for the agent set (kill-switch, #819)
            game_settings_client: OpenFeature client for the "game" domain, used to
                resolve effective (agent-override-aware) values for the truthful
                admin display (#818).
            agent_settings_client: OpenFeature client for the "agent" domain, used
                to read the effective ``interventions_allowed`` level (#819).
            select_game_mode: async ``(game_name, *, source, serial)`` callback that
                applies a game-mode selection everywhere the menu reads it (the
                servicer's ``current_selection``, this manager's mode, the persisted
                ``current_game`` preference) and publishes ``selection_changed``.
                Provided by the servicer (#1272).
        """
        self.led = led
        self.audio = audio
        self.game_settings_writer = game_settings_writer
        self.user_prefs_writer = user_prefs_writer
        self.agent_settings_writer = agent_settings_writer
        self.game_settings_client = game_settings_client
        self.agent_settings_client = agent_settings_client
        self.publish_event = publish_event
        self._select_game_mode = select_game_mode

        # Controller state tracking - single source of truth
        # Keys = connected controllers, values = their current state
        self.controller_states: dict[str, ControllerState] = {}

        # Battery level tracking (0-5 scale, updated from button events)
        self.battery_levels: dict[str, int] = {}

        # Button state tracking (for combo detection)
        self.button_states: dict[str, dict[str, bool]] = {}

        # Handlers for each state
        self._handlers: dict[ControllerState, ControllerHandler] = {}

        # Current game mode (for LED colors)
        self.current_game_mode: Games = Games.JoustFFA

    @property
    def connected_controllers(self) -> set[str]:
        """All connected controller serials (computed from controller_states)."""
        return set(self.controller_states.keys())

    @property
    def ready_controllers(self) -> set[str]:
        """Controller serials in READY state (computed from controller_states)."""
        return {serial for serial, state in self.controller_states.items() if state == ControllerState.READY}

    def _abandon_admin_session(self) -> None:
        """UC-012 BR-004: release the admin session (single-admin lock) when state is cleared wholesale."""
        admin_handler = self._handlers.get(ControllerState.ADMIN)
        if admin_handler is not None and hasattr(admin_handler, "abandon_session"):
            admin_handler.abandon_session()

    def register_handler(self, handler: ControllerHandler) -> None:
        """
        Register a handler for a controller state.

        Args:
            handler: Handler instance implementing ControllerHandler protocol
        """
        handler.set_state_manager(self)
        self._handlers[handler.state] = handler
        logger.debug(f"Registered handler for state: {handler.state.value}")

    def get_handler(self, state: ControllerState) -> ControllerHandler | None:
        """
        Get the handler for a state.

        Args:
            state: Controller state

        Returns:
            Handler instance or None if not registered
        """
        return self._handlers.get(state)

    def get_controller_state(self, serial: str) -> ControllerState:
        """
        Get the current state of a controller.

        Args:
            serial: Controller serial number

        Returns:
            Current state (defaults to CONNECTED for unknown controllers)
        """
        return self.controller_states.get(serial, ControllerState.CONNECTED)

    async def handle_button_event(
        self, serial: str, button: str, is_press: bool, event_time: float | None = None
    ) -> None:
        """
        Handle a button event for a controller.

        Dispatches to the appropriate handler based on current state.

        Args:
            serial: Controller serial number
            button: Button name
            is_press: True if press, False if release
            event_time: When the press happened (seconds), for debouncing; None = processing time
        """
        # Track controller as connected
        if serial not in self.connected_controllers:
            await self.on_controller_connected(serial)

        # Update button state
        if serial not in self.button_states:
            self.button_states[serial] = {
                "trigger": False,
                "move": False,
                "cross": False,
                "circle": False,
                "square": False,
                "triangle": False,
                "ps": False,
                "select": False,
                "start": False,
            }
        self.button_states[serial][button] = is_press

        # Handle release events - forward trigger releases in admin mode
        if not is_press:
            if button == ButtonTrackingKey.TRIGGER:
                state = self.get_controller_state(serial)
                if state == ControllerState.ADMIN:
                    handler = self._handlers.get(state)
                    if handler and hasattr(handler, "handle_trigger_release"):
                        await handler.handle_trigger_release(serial)
            return

        # Dispatch to appropriate handler
        # Note: Admin mode is handled externally by AdminModeHandler
        state = self.get_controller_state(serial)
        handler = self._handlers.get(state)
        if handler:
            await handler.handle_button(serial, button, event_time)
        else:
            logger.warning(f"No handler for state {state.value}")

    async def transition_to(self, serial: str, new_state: ControllerState) -> None:
        """
        Transition a controller to a new state.

        Calls on_exit on old handler and on_enter on new handler.

        Args:
            serial: Controller serial number
            new_state: State to transition to
        """
        old_state = self.controller_states.get(serial, ControllerState.CONNECTED)

        if old_state == new_state:
            return

        # Call exit handler
        old_handler = self._handlers.get(old_state)
        if old_handler:
            await old_handler.on_exit(serial)

        # Update state
        self.controller_states[serial] = new_state

        # Emit metrics for ready state changes
        if new_state == ControllerState.READY:
            metrics.player_ready.labels(serial=serial).set(1)
        elif old_state == ControllerState.READY:
            # Only set to 0 if transitioning FROM ready
            metrics.player_ready.labels(serial=serial).set(0)

        # Call enter handler
        new_handler = self._handlers.get(new_state)
        if new_handler:
            await new_handler.on_enter(serial)

        logger.info(f"Controller {serial} transitioned: {old_state.value} -> {new_state.value}")

    async def on_controller_connected(self, serial: str) -> None:
        """
        Handle a new controller connection.

        Args:
            serial: Controller serial number
        """
        self.controller_states[serial] = ControllerState.CONNECTED

        # Call enter handler for CONNECTED state
        handler = self._handlers.get(ControllerState.CONNECTED)
        if handler:
            await handler.on_enter(serial)

        logger.info(f"Controller {serial} connected")

    async def on_controller_disconnected(self, serial: str) -> None:
        """
        Handle a controller disconnection.

        Args:
            serial: Controller serial number
        """
        # Call exit handler for current state
        state = self.controller_states.get(serial, ControllerState.CONNECTED)
        handler = self._handlers.get(state)
        if handler:
            await handler.on_exit(serial)

        # Clear ready metric if player was ready
        if state == ControllerState.READY:
            metrics.player_ready.labels(serial=serial).set(0)

        # Clean up state (removing from controller_states also removes from computed properties)
        self.controller_states.pop(serial, None)
        self.button_states.pop(serial, None)
        self.battery_levels.pop(serial, None)

        logger.info(f"Controller {serial} disconnected")

    async def on_stream_reconnected(self) -> None:
        """
        Clear all controller state on stream reconnect.

        When the button event stream reconnects, the backend will replay
        initial connection events for all currently connected controllers.
        Clear state so these events re-populate cleanly without ghosts.
        """
        count = len(self.controller_states)
        self._abandon_admin_session()
        self.controller_states.clear()
        self.button_states.clear()
        self.battery_levels.clear()
        metrics.player_ready.clear()
        logger.info(f"Stream reconnected: cleared {count} controller(s) for re-population")

    def update_battery(self, serial: str, battery: int) -> None:
        """
        Update battery level for a controller.

        Args:
            serial: Controller serial number
            battery: Battery level (0-5 scale from psmove)
        """
        self.battery_levels[serial] = battery

    def get_ready_count(self) -> int:
        """Get number of ready controllers."""
        return len(self.ready_controllers)

    def get_connected_count(self) -> int:
        """Get number of connected controllers."""
        return len(self.connected_controllers)

    def all_ready(self) -> bool:
        """Check if all connected controllers are ready (minimum 2)."""
        return len(self.ready_controllers) >= 2 and len(self.ready_controllers) == len(self.connected_controllers)

    def set_game_mode(self, game_mode: Games) -> None:
        """
        Set the current game mode.

        Args:
            game_mode: Games enum value
        """
        self.current_game_mode = game_mode

    async def select_game_mode(self, game_mode: Games, *, source: str, serial: str | None = None) -> None:
        """
        Select a game mode for the next game and persist it (#1272).

        Delegates to the servicer callback so the controller SELECT path lands in
        the same state as admin Cross / web ``select_game`` (the coordinator start
        path reads the servicer's ``current_selection``). Without a callback
        (test-only: production always wires the servicer callback in
        ``MenuServicer.__init__``), updates the local mode, persists
        ``current_game`` and publishes ``selection_changed`` directly. Keep this
        fallback in step with ``MenuServicer.select_game_mode``.

        Args:
            game_mode: Games enum value to select
            source: Who made the selection (e.g. "controller", "admin", "web")
            serial: Controller serial that made the selection, if any
        """
        if self._select_game_mode is not None:
            await self._select_game_mode(game_mode.name, source=source, serial=serial)
            return

        self.set_game_mode(game_mode)
        await self.show_lobby_colors()
        await asyncio.to_thread(self.user_prefs_writer.update_default_variant, "current_game", game_mode.name)
        payload = {"game_name": game_mode.name, "source": source}
        if serial is not None:
            payload["serial"] = serial
        await self.publish_event("selection_changed", payload)

    async def show_lobby_colors(self) -> None:
        """UC-011 step 6, BR-005: the current mode's lobby colour on every CONNECTED (dim) and READY
        (bright) controller; ADMIN and IDLE controllers keep theirs. Best effort per controller."""
        mode = self.current_game_mode.name
        for serial, state in list(self.controller_states.items()):
            try:
                if state == ControllerState.CONNECTED:
                    await self.led.set_connected_color(serial, mode)
                elif state == ControllerState.READY:
                    await self.led.set_ready_color(serial, mode)
            except Exception as e:
                logger.warning(f"Lobby colour for {serial} failed: {e}")

    async def reset(self) -> list[str]:
        """
        Reset all controller states (e.g., after game ends).

        Re-registers all previously connected controllers, triggering on_enter
        handlers to set appropriate lobby colors.

        Returns:
            List of controller serials that were re-registered
        """
        # Remember which controllers were connected before clearing
        serials = list(self.controller_states.keys())

        # Clearing state skips on_exit, so release the admin session explicitly.
        self._abandon_admin_session()

        # Clear all state (controller_states is the single source of truth)
        self.controller_states.clear()
        self.button_states.clear()

        # Clear all player ready metrics
        metrics.player_ready.clear()

        # Reset ready handler's game start flag (Issue #230)
        ready_handler = self._handlers.get(ControllerState.READY)
        if ready_handler and hasattr(ready_handler, "reset_game_start_flag"):
            ready_handler.reset_game_start_flag()

        # Re-register each controller as CONNECTED (triggers on_enter -> sets colors)
        for serial in serials:
            await self.on_controller_connected(serial)

        logger.info(f"StateManager reset, re-registered {len(serials)} controllers")
        return serials
