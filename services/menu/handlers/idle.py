"""Handler for controllers in the idle state."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from services.menu.handlers.base import ControllerState

if TYPE_CHECKING:
    from services.menu.state_manager import StateManager

logger = logging.getLogger(__name__)


class IdleHandler:
    """
    Handles button events for controllers in the idle state.

    Controllers enter this state after prolonged lobby inactivity to save battery.
    LEDs are turned off. Any button press wakes the controller back to CONNECTED
    and triggers a wake-all via the idle monitor.

    Button mappings:
    - Any button: Wake controller (transition to CONNECTED)
    """

    def __init__(self, wake_callback=None):
        """
        Initialize idle handler.

        Args:
            wake_callback: Async function called when a controller wakes from idle.
                          Signature: async (serial: str) -> None
        """
        self._state_manager: StateManager | None = None
        self._wake_callback = wake_callback

    @property
    def state(self) -> ControllerState:
        """The state this handler manages."""
        return ControllerState.IDLE

    def set_state_manager(self, manager: StateManager) -> None:
        """Set the state manager reference."""
        self._state_manager = manager

    def set_wake_callback(self, callback) -> None:
        """Set the wake callback (for deferred wiring)."""
        self._wake_callback = callback

    async def handle_button(self, serial: str, button: str) -> None:
        """
        Handle a button press event — any button wakes the controller.

        Args:
            serial: Controller serial number
            button: Button name
        """
        if self._state_manager is None:
            logger.error("StateManager not set")
            return

        logger.info(f"Controller {serial} waking from idle (button: {button})")

        # Transition back to connected state
        await self._state_manager.transition_to(serial, ControllerState.CONNECTED)

        # Notify idle monitor to wake all other idle controllers
        if self._wake_callback:
            await self._wake_callback(serial)

    async def on_enter(self, serial: str) -> None:
        """
        Called when a controller enters the idle state.

        Turns off LEDs to save battery.
        """
        if self._state_manager is None:
            return

        await self._state_manager.led.set_color(serial, (0, 0, 0))
        logger.debug(f"Controller {serial} entered idle state (LEDs off)")

    async def on_exit(self, serial: str) -> None:
        """
        Called when a controller exits the idle state.

        LED restoration is handled by the target state's on_enter.
        """
        logger.debug(f"Controller {serial} exiting idle state")
