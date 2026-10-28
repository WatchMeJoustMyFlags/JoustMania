"""Handler for controllers in the connected (not ready) state."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from services.menu.handlers.base import ButtonDebouncer, ControllerState
from services.menu.utils.settings import get_next_game_mode

if TYPE_CHECKING:
    from services.menu.state_manager import StateManager

logger = logging.getLogger(__name__)


class ConnectedHandler:
    """
    Handles button events for controllers in the connected state.

    Controllers in this state are connected but haven't pressed trigger to ready up.
    They have dim LED colors based on the current game mode.

    Button mappings:
    - Trigger: Transition to ready state
    - Select: Cycle game modes
    """

    def __init__(self):
        """Initialize connected handler."""
        self._state_manager: StateManager | None = None
        self._debouncer = ButtonDebouncer(default_interval=0.1)
        # Ready beeps in flight: held so they are not garbage-collected (#2312).
        self._sound_tasks: set[asyncio.Task] = set()

    @property
    def state(self) -> ControllerState:
        """The state this handler manages."""
        return ControllerState.CONNECTED

    def set_state_manager(self, manager: StateManager) -> None:
        """Set the state manager reference."""
        self._state_manager = manager

    async def handle_button(self, serial: str, button: str, event_time: float | None = None) -> None:
        """
        Handle a button press event.

        Args:
            serial: Controller serial number
            button: Button name
            event_time: When the press happened (seconds), for debouncing; None = processing time
        """
        if self._state_manager is None:
            logger.error("StateManager not set")
            return

        if button == "trigger":
            if not self._debouncer.should_process(serial, "trigger", event_time=event_time):
                return
            await self._handle_trigger(serial)

        elif button == "select":
            if not self._debouncer.should_process(serial, "select", event_time=event_time):
                return
            await self._handle_select(serial)

    async def on_enter(self, serial: str) -> None:
        """
        Called when a controller enters the connected state.

        Sets the LED to dim game mode color.
        """
        if self._state_manager is None:
            return

        await self._state_manager.led.set_connected_color(
            serial,
            self._state_manager.current_game_mode.name,
        )
        logger.debug(f"Controller {serial} entered connected state")

    async def on_exit(self, serial: str) -> None:
        """
        Called when a controller exits the connected state.
        """
        logger.debug(f"Controller {serial} exiting connected state")

    async def _handle_trigger(self, serial: str) -> None:
        """
        Handle trigger press - transition to ready state.

        Args:
            serial: Controller serial number
        """
        if self._state_manager is None:
            return

        logger.info(f"Controller {serial} trigger press -> ready")

        # READY and its LED first (UC-010 step 2, BR-012): the button events of every
        # controller are handled one after another, so awaiting the beep here held the
        # next press (another ready, an un-ready) back for as long as audio took (#2312).
        await self._state_manager.transition_to(serial, ControllerState.READY)
        self._play_ready_beep_detached()

    def _play_ready_beep_detached(self) -> None:
        """Play the ready beep without awaiting it (pattern of AdminHandler._play_voice_detached, #1746)."""
        task = asyncio.get_running_loop().create_task(self._play_ready_beep())
        self._sound_tasks.add(task)
        task.add_done_callback(self._sound_tasks.discard)

    async def _play_ready_beep(self) -> None:
        if self._state_manager is None:
            return
        from lib.types import Sound

        try:
            await self._state_manager.audio.play_sound(Sound.SFX_BEEP_LOUD, volume=0.5)
        except Exception as e:
            logger.warning(f"Ready beep failed: {e}")

    async def _handle_select(self, serial: str) -> None:
        """
        Handle select button press - cycle game modes.

        Args:
            serial: Controller serial number
        """
        if self._state_manager is None:
            return

        # Cycle to next playable game mode and apply it through the same path as
        # admin Cross / web select (servicer selection + state manager + persisted
        # current_game + selection_changed event). #1272: this used to call a
        # non-existent ``state_manager.settings`` and crash the button stream.
        next_mode = get_next_game_mode(self._state_manager.current_game_mode, forward=True)
        await self._state_manager.select_game_mode(next_mode, source="controller", serial=serial)

        # Play voice announcement
        await self._state_manager.audio.play_game_mode_voice(next_mode.name)

        logger.info(f"Controller {serial} select button -> game mode: {next_mode.name}")
