"""Unit tests for controller state handlers."""

import time
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from lib.flag_service_writer import FlagServiceWriter
from lib.types import Games
from services.menu.handlers.base import ButtonDebouncer, ControllerState
from services.menu.handlers.connected import ConnectedHandler
from services.menu.handlers.ready import ReadyHandler
from services.menu.state_manager import StateManager
from services.menu.utils import AudioHelper, LedController
from services.menu.utils.settings import GAME_MODES, get_next_game_mode


class TestButtonDebouncer:
    """Tests for ButtonDebouncer utility class."""

    def test_first_press_always_processed(self):
        """First button press should always be processed."""
        debouncer = ButtonDebouncer(default_interval=0.1)
        assert debouncer.should_process("serial1", "trigger") is True

    def test_rapid_press_debounced(self):
        """Rapid presses within interval should be debounced."""
        debouncer = ButtonDebouncer(default_interval=0.1)

        # First press passes
        assert debouncer.should_process("serial1", "trigger") is True
        # Immediate second press blocked
        assert debouncer.should_process("serial1", "trigger") is False

    def test_press_after_interval_processed(self):
        """Press after interval should be processed."""
        debouncer = ButtonDebouncer(default_interval=0.01)  # 10ms for fast test

        assert debouncer.should_process("serial1", "trigger") is True

        # Wait for interval to pass
        time.sleep(0.015)

        assert debouncer.should_process("serial1", "trigger") is True

    def test_different_buttons_independent(self):
        """Different buttons should have independent debounce state."""
        debouncer = ButtonDebouncer(default_interval=0.1)

        assert debouncer.should_process("serial1", "trigger") is True
        assert debouncer.should_process("serial1", "move") is True  # Different button
        assert debouncer.should_process("serial1", "trigger") is False  # Same button blocked

    def test_different_controllers_independent(self):
        """Different controllers should have independent debounce state."""
        debouncer = ButtonDebouncer(default_interval=0.1)

        assert debouncer.should_process("serial1", "trigger") is True
        assert debouncer.should_process("serial2", "trigger") is True  # Different controller
        assert debouncer.should_process("serial1", "trigger") is False  # Same controller blocked

    def test_custom_interval_override(self):
        """Custom interval override should be respected."""
        debouncer = ButtonDebouncer(default_interval=0.01)

        assert debouncer.should_process("serial1", "trigger") is True
        time.sleep(0.015)  # Wait past default interval

        # Use longer override interval - should still be blocked
        assert debouncer.should_process("serial1", "trigger", interval=0.5) is False

    def test_clear_single_controller(self):
        """clear() with serial should clear only that controller."""
        debouncer = ButtonDebouncer(default_interval=0.1)

        debouncer.should_process("serial1", "trigger")
        debouncer.should_process("serial2", "trigger")

        debouncer.clear("serial1")

        # serial1 cleared - can press again
        assert debouncer.should_process("serial1", "trigger") is True
        # serial2 not cleared - still blocked
        assert debouncer.should_process("serial2", "trigger") is False

    def test_clear_all(self):
        """clear() without args should clear all state."""
        debouncer = ButtonDebouncer(default_interval=0.1)

        debouncer.should_process("serial1", "trigger")
        debouncer.should_process("serial2", "trigger")

        debouncer.clear()

        # Both can press again
        assert debouncer.should_process("serial1", "trigger") is True
        assert debouncer.should_process("serial2", "trigger") is True

    def test_default_interval(self):
        """Default interval should be 100ms."""
        debouncer = ButtonDebouncer()
        assert debouncer.default_interval == 0.1


class TestConnectedHandler:
    """Tests for ConnectedHandler."""

    @pytest.fixture
    def handler(self):
        """Create ConnectedHandler instance."""
        return ConnectedHandler()

    @pytest.fixture
    def mock_state_manager(self):
        """Real StateManager over spec'd collaborators.

        #1272: this used to be a bare MagicMock with ``manager.settings`` grafted
        on, so the handler's call to a non-existent ``StateManager.settings``
        passed. A real StateManager (with ``spec_set`` autospec'd LED, audio and
        flag writers) makes any call to an API that does not exist fail.
        """
        return StateManager(
            led=create_autospec(LedController, instance=True, spec_set=True),
            audio=create_autospec(AudioHelper, instance=True, spec_set=True),
            game_settings_writer=create_autospec(FlagServiceWriter, instance=True, spec_set=True),
            user_prefs_writer=create_autospec(FlagServiceWriter, instance=True, spec_set=True),
            publish_event=AsyncMock(),
        )

    def test_state_property(self, handler):
        """Handler should return CONNECTED state."""
        assert handler.state == ControllerState.CONNECTED

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="main", steps=[6])
    async def test_on_enter_sets_dim_color(self, handler, mock_state_manager):
        """on_enter should set dim LED color."""
        handler.set_state_manager(mock_state_manager)

        await handler.on_enter("serial1")

        mock_state_manager.led.set_connected_color.assert_called_once_with("serial1", "JoustFFA")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="main", steps=[2])
    async def test_handle_button_trigger(self, handler, mock_state_manager):
        """Trigger press should transition to READY."""
        mock_state_manager.register_handler(handler)
        handler._debouncer.should_process = MagicMock(return_value=True)

        await handler.handle_button("serial1", "trigger")

        assert mock_state_manager.get_controller_state("serial1") == ControllerState.READY

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="A2")
    async def test_handle_button_select_cycles_game(self, handler, mock_state_manager):
        """UC-011 A2 / FR-011 (#1272): SELECT in the lobby advances to the next playable mode.

        It applies the selection on the state manager, persists ``current_game``,
        publishes ``selection_changed`` and announces the new mode.
        """
        mock_state_manager.register_handler(handler)
        handler._debouncer.should_process = MagicMock(return_value=True)
        expected = get_next_game_mode(Games.JoustFFA, forward=True)
        assert expected != Games.JoustFFA

        await handler.handle_button("serial1", "select")

        assert mock_state_manager.current_game_mode == expected
        mock_state_manager.user_prefs_writer.update_default_variant.assert_called_once_with(
            "current_game", expected.name
        )
        mock_state_manager.publish_event.assert_awaited_once_with(
            "selection_changed", {"game_name": expected.name, "source": "controller", "serial": "serial1"}
        )
        mock_state_manager.audio.play_game_mode_voice.assert_awaited_once_with(expected.name)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="A2", br=["BR-001"])
    async def test_handle_button_select_wraps_around(self, handler, mock_state_manager):
        """Repeated SELECT presses cycle through every playable mode and wrap (UC-011 BR-001)."""
        mock_state_manager.register_handler(handler)
        handler._debouncer.should_process = MagicMock(return_value=True)

        seen = []
        for _ in GAME_MODES:
            await handler.handle_button("serial1", "select")
            seen.append(mock_state_manager.current_game_mode.name)

        assert sorted(seen) == sorted(GAME_MODES)
        assert mock_state_manager.current_game_mode == Games.JoustFFA

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="A2")
    async def test_handle_button_select_uses_selection_callback(self, handler):
        """With a servicer callback wired, SELECT routes through it (single selection path)."""
        select_cb = AsyncMock()
        manager = StateManager(
            led=create_autospec(LedController, instance=True, spec_set=True),
            audio=create_autospec(AudioHelper, instance=True, spec_set=True),
            game_settings_writer=create_autospec(FlagServiceWriter, instance=True, spec_set=True),
            user_prefs_writer=create_autospec(FlagServiceWriter, instance=True, spec_set=True),
            publish_event=AsyncMock(),
            select_game_mode=select_cb,
        )
        manager.register_handler(handler)
        handler._debouncer.should_process = MagicMock(return_value=True)
        expected = get_next_game_mode(Games.JoustFFA, forward=True)

        await handler.handle_button("serial1", "select")

        select_cb.assert_awaited_once_with(expected.name, source="controller", serial="serial1")
        manager.user_prefs_writer.update_default_variant.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_button_debounce(self, handler, mock_state_manager):
        """Rapid button presses should be debounced."""
        mock_state_manager.register_handler(handler)
        # First call returns True, second returns False (debounced)
        handler._debouncer.should_process = MagicMock(side_effect=[True, False])
        mock_state_manager.transition_to = AsyncMock()

        await handler.handle_button("serial1", "trigger")
        await handler.handle_button("serial1", "trigger")

        assert mock_state_manager.transition_to.call_count == 1


class TestReadyHandler:
    """Tests for ReadyHandler."""

    @pytest.fixture
    def start_game_callback(self):
        """Create mock start game callback."""
        return AsyncMock()

    @pytest.fixture
    def handler(self, start_game_callback):
        """Create ReadyHandler instance."""
        return ReadyHandler(start_game_callback)

    @pytest.fixture
    def mock_state_manager(self):
        """Create mock StateManager."""
        from lib.types import Games

        manager = MagicMock()
        manager.led = MagicMock()
        manager.led.set_ready_color = AsyncMock()
        manager.transition_to = AsyncMock()
        manager.current_game_mode = Games.JoustFFA
        manager.all_ready = MagicMock(return_value=False)
        manager.get_ready_count = MagicMock(return_value=1)
        manager.get_connected_count = MagicMock(return_value=2)
        return manager

    def test_state_property(self, handler):
        """Handler should return READY state."""
        assert handler.state == ControllerState.READY

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="main", steps=[2])
    async def test_on_enter_sets_bright_color(self, handler, mock_state_manager):
        """on_enter should set bright LED color."""
        handler.set_state_manager(mock_state_manager)

        await handler.on_enter("serial1")

        mock_state_manager.led.set_ready_color.assert_called_once_with("serial1", "JoustFFA")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="main", steps=[4])
    async def test_on_enter_starts_game_when_all_ready(self, handler, mock_state_manager, start_game_callback):
        """on_enter should start game when all controllers are ready."""
        mock_state_manager.all_ready.return_value = True
        handler.set_state_manager(mock_state_manager)

        await handler.on_enter("serial1")

        start_game_callback.assert_called_once_with("serial1")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A1")
    async def test_handle_button_move_unreadies(self, handler, mock_state_manager):
        """Move press should transition back to CONNECTED."""
        handler.set_state_manager(mock_state_manager)
        handler._debouncer.should_process = MagicMock(return_value=True)

        await handler.handle_button("serial1", "move")

        mock_state_manager.transition_to.assert_called_once_with("serial1", ControllerState.CONNECTED)
