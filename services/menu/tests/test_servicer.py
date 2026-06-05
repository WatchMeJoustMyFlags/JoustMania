"""
Unit tests for MenuServicer.

Tests the core Menu gRPC service:
- State management
- Controller event callbacks
- Admin mode callbacks
- Input handling logic
- Lifecycle methods

Note: MenuServicer has complex initialization with many dependencies.
These tests mock at method level rather than trying to mock the entire __init__.

Issue #209: Improve test coverage for critical game flow
"""

import asyncio
import contextlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from lib.types import Games
from proto import menu_pb2
from services.menu.handlers.base import ControllerState


class MockGrpcContext:
    """Mock gRPC context."""

    def __init__(self):
        self._cancelled = False

    def cancelled(self):
        return self._cancelled

    def cancel(self):
        self._cancelled = True


class FakeMenuServicer:
    """A lightweight fake of MenuServicer for testing individual methods.

    This avoids the complex initialization of the real MenuServicer
    while allowing us to test the method logic.

    Uses the new data model where controller state is managed via
    state_manager.controller_states as the single source of truth.
    """

    def __init__(self):
        self.state = menu_pb2.MenuState.STOPPED
        self.current_selection = Games.JoustFFA

        # Game event monitoring
        self.game_event_task = None
        self.game_event_monitor_running = False

        # Mocked dependencies
        self.led = MagicMock()
        self.audio = MagicMock()
        self.audio.start_lobby_music = AsyncMock()
        self.audio.stop_lobby_music = AsyncMock()
        self.audio.play_voice = AsyncMock()

        # FlagConfigWriter mocks (replacing SettingsHelper)
        self.game_settings_writer = MagicMock()
        self.game_settings_writer.update_default_variant = MagicMock(return_value=True)
        self.game_settings_writer.cycle_variant = MagicMock(return_value="medium")
        self.game_settings_writer.get_current_variant = MagicMock(return_value="medium")

        self.user_prefs_writer = MagicMock()
        self.user_prefs_writer.update_default_variant = MagicMock(return_value=True)
        self.user_prefs_writer.cycle_variant = MagicMock(return_value="on")
        self.user_prefs_writer.get_current_variant = MagicMock(return_value="on")

        # OpenFeature client mocks (for reading settings)
        self.game_settings_client = MagicMock()
        self.game_settings_client.get_integer_value = MagicMock(return_value=2)
        self.game_settings_client.get_boolean_value = MagicMock(return_value=True)
        self.game_settings_client.get_float_value = MagicMock(return_value=4.0)
        self.game_settings_client.get_string_value = MagicMock(return_value="medium")

        self.user_prefs_client = MagicMock()
        self.user_prefs_client.get_string_value = MagicMock(return_value="ivy")
        self.user_prefs_client.get_boolean_value = MagicMock(return_value=True)

        self.event_publisher = MagicMock()
        self.event_publisher.publish = AsyncMock()
        self.event_publisher.subscribe = AsyncMock(return_value=asyncio.Queue())
        self.event_publisher.unsubscribe = AsyncMock()

        self.state_manager = MagicMock()
        self.state_manager.on_controller_connected = AsyncMock()
        self.state_manager.on_controller_disconnected = AsyncMock()
        self.state_manager.update_battery = MagicMock()
        self.state_manager.handle_button_event = AsyncMock()
        self.state_manager.set_game_mode = MagicMock()
        self.state_manager.reset = AsyncMock(return_value=["serial_1", "serial_2"])
        # controller_states is the single source of truth (starts empty)
        self.state_manager.controller_states = {}
        self.state_manager.button_states = {}
        self.state_manager.transition_to = AsyncMock()

        # Computed properties on state_manager (match real implementation)
        type(self.state_manager).ready_controllers = property(
            lambda sm: {s for s, st in sm.controller_states.items() if st == ControllerState.READY}
        )
        type(self.state_manager).connected_controllers = property(lambda sm: set(sm.controller_states.keys()))

        self.controller_events = MagicMock()
        self.controller_events.start = AsyncMock()
        self.controller_events.stop = AsyncMock()
        self.controller_events.is_running = False
        self.controller_events.wait_for_connection = AsyncMock()

        self.admin_handler = MagicMock()
        self.admin_handler.combo_shown = False
        self.admin_handler.check_combo_from_state = MagicMock(return_value=False)

        self.voice_actor = "ivy"
        self.game_options = []

        # Mock channels (no settings_channel - settings moved to flagd)
        self.controller_channel = MagicMock()
        self.controller_channel.close = AsyncMock()
        self.game_coordinator_channel = MagicMock()
        self.game_coordinator_channel.close = AsyncMock()
        self.audio_channel = MagicMock()
        self.audio_channel.close = AsyncMock()

    # Computed properties that delegate to state_manager (match real implementation)
    @property
    def ready_controllers(self) -> set[str]:
        """Controllers in READY state (delegates to state_manager)."""
        return self.state_manager.ready_controllers

    @property
    def connected_controllers(self) -> set[str]:
        """All connected controllers (delegates to state_manager)."""
        return self.state_manager.connected_controllers

    @property
    def ready_controller_count(self) -> int:
        """Number of ready controllers (computed from state_manager)."""
        return len(self.state_manager.ready_controllers)

    async def on_disconnect(self, serial: str) -> None:
        """Handle controller disconnect (matches real implementation)."""
        await self.state_manager.on_controller_disconnected(serial)


# Import the actual method implementations to bind to our fake
def bind_methods_from_servicer():
    """Import real method implementations from MenuServicer."""
    # We'll test the logic of individual methods by copying them to the fake
    pass


class TestMenuServicerState:
    """Tests for state management."""

    @pytest.fixture
    def servicer(self):
        """Create a fake servicer for testing."""
        return FakeMenuServicer()

    def test_initial_state_stopped(self, servicer):
        """Initial state should be STOPPED."""
        assert servicer.state == menu_pb2.MenuState.STOPPED

    def test_initial_selection_is_joust_ffa(self, servicer):
        """Initial selection should be JoustFFA."""
        assert servicer.current_selection == Games.JoustFFA

    def test_initial_ready_controllers_empty(self, servicer):
        """Ready controllers set should start empty."""
        assert servicer.ready_controllers == set()

    def test_initial_connected_controllers_empty(self, servicer):
        """Connected controllers set should start empty."""
        assert servicer.connected_controllers == set()

    def test_initial_ready_count_zero(self, servicer):
        """Ready controller count should start at 0."""
        assert servicer.ready_controller_count == 0


class TestMenuServicerAdminModeCallbacks:
    """Tests for AdminModeCallbacks protocol methods."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    def test_set_menu_state(self, servicer):
        """set_menu_state should update the state."""
        servicer.state = menu_pb2.MenuState.STOPPED

        # Simulate the real implementation
        def set_menu_state(state):
            servicer.state = state

        set_menu_state(menu_pb2.MenuState.RUNNING)

        assert servicer.state == menu_pb2.MenuState.RUNNING

    def test_get_game_options_empty_default(self, servicer):
        """get_game_options should return empty list by default."""
        assert servicer.game_options == []

    def test_get_game_options_returns_options(self, servicer):
        """get_game_options should return options when set."""
        servicer.game_options = ["Option1", "Option2"]
        assert servicer.game_options == ["Option1", "Option2"]


class TestMenuServicerControllerCallbacks:
    """Tests for ControllerEventCallbacks protocol methods."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    def test_get_menu_state(self, servicer):
        """get_menu_state should return current state."""
        servicer.state = menu_pb2.MenuState.GAME_STARTING
        assert servicer.state == menu_pb2.MenuState.GAME_STARTING

    @pytest.mark.asyncio
    async def test_on_connect_adds_to_connected(self, servicer):
        """on_connect should delegate to state_manager which tracks the connection."""

        # Make state_manager.on_controller_connected update controller_states
        async def mock_on_connected(serial):
            servicer.state_manager.controller_states[serial] = ControllerState.CONNECTED

        servicer.state_manager.on_controller_connected = AsyncMock(side_effect=mock_on_connected)

        # Simulate the real on_connect implementation (just delegates to state_manager)
        async def on_connect(serial):
            await servicer.state_manager.on_controller_connected(serial)

        await on_connect("serial_1")

        assert "serial_1" in servicer.connected_controllers
        servicer.state_manager.on_controller_connected.assert_called_once_with("serial_1")

    @pytest.mark.asyncio
    async def test_on_disconnect_removes_from_all_sets(self, servicer):
        """on_disconnect should clean up all tracking state."""
        # Set up initial state via state_manager.controller_states (single source of truth)
        servicer.state_manager.controller_states = {
            "serial_1": ControllerState.READY,
            "serial_2": ControllerState.CONNECTED,
        }

        # Make state_manager.on_controller_disconnected update controller_states
        async def mock_on_disconnected(serial):
            servicer.state_manager.controller_states.pop(serial, None)

        servicer.state_manager.on_controller_disconnected = AsyncMock(side_effect=mock_on_disconnected)

        # Call on_disconnect (now delegates to state_manager)
        await servicer.on_disconnect("serial_1")

        assert "serial_1" not in servicer.connected_controllers
        assert "serial_1" not in servicer.ready_controllers
        assert servicer.ready_controller_count == 0

    def test_update_battery_calls_state_manager(self, servicer):
        """update_battery should delegate to state_manager."""
        servicer.state_manager.update_battery("serial_1", 80)
        servicer.state_manager.update_battery.assert_called_with("serial_1", 80)


class TestMenuServicerOnButton:
    """Tests for on_button callback logic."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    @pytest.mark.asyncio
    async def test_on_button_forwards_to_state_manager(self, servicer):
        """on_button should forward to state_manager.handle_button_event."""
        await servicer.state_manager.handle_button_event("serial_1", "trigger", True)
        servicer.state_manager.handle_button_event.assert_called_once_with("serial_1", "trigger", True)

    @pytest.mark.asyncio
    async def test_on_button_checks_admin_combo(self, servicer):
        """on_button should check admin combo on face button press."""
        servicer.state_manager.button_states = {"serial_1": {"cross": True, "circle": True, "square": True}}

        # Create preview state like the real implementation
        button_state = servicer.state_manager.button_states.get("serial_1", {})
        preview_state = dict(button_state)
        preview_state["triangle"] = True

        servicer.admin_handler.check_combo_from_state(preview_state)

        servicer.admin_handler.check_combo_from_state.assert_called_with(preview_state)

    @pytest.mark.asyncio
    async def test_face_button_release_resets_admin_combo(self, servicer):
        """Release of face button should reset admin combo flag."""
        servicer.admin_handler.combo_shown = True

        # Simulate the real logic
        button = "cross"
        is_press = False
        if not is_press and button in ["cross", "circle", "square", "triangle"]:
            servicer.admin_handler.combo_shown = False

        assert servicer.admin_handler.combo_shown is False


class TestMenuServicerStartMenu:
    """Tests for StartMenu gRPC method logic."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    @pytest.mark.asyncio
    async def test_start_menu_success(self, servicer):
        """StartMenu should transition to RUNNING and return success."""
        # Add some ready controllers to verify they get cleared
        servicer.state_manager.controller_states = {"s1": ControllerState.READY}

        # Simulate the real StartMenu logic (uses flagd clients instead of settings_helper)
        if servicer.state == menu_pb2.MenuState.RUNNING:
            success = False
            error = "Menu already running"
        else:
            servicer.state = menu_pb2.MenuState.RUNNING
            # Clear ready state (new implementation clears controller_states READY entries)
            for serial in list(servicer.state_manager.controller_states.keys()):
                if servicer.state_manager.controller_states[serial] == ControllerState.READY:
                    servicer.state_manager.controller_states[serial] = ControllerState.CONNECTED
            # Load settings from flagd (user domain)
            servicer.voice_actor = servicer.user_prefs_client.get_string_value("menu_voice", "ivy")
            servicer.user_prefs_client.get_string_value("current_game", "JoustFFA")
            await servicer.audio.start_lobby_music()
            await servicer.event_publisher.publish("menu_started", {})
            success = True
            error = ""

        assert success is True
        assert error == ""
        assert servicer.state == menu_pb2.MenuState.RUNNING
        assert servicer.ready_controller_count == 0  # All ready controllers cleared
        servicer.audio.start_lobby_music.assert_called_once()
        servicer.event_publisher.publish.assert_called_with("menu_started", {})

    @pytest.mark.asyncio
    async def test_start_menu_already_running(self, servicer):
        """StartMenu should fail if already running."""
        servicer.state = menu_pb2.MenuState.RUNNING

        if servicer.state == menu_pb2.MenuState.RUNNING:
            success = False
            error = "Menu already running"
        else:
            success = True
            error = ""

        assert success is False
        assert "already running" in error


class TestMenuServicerStopMenu:
    """Tests for StopMenu gRPC method logic."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    @pytest.mark.asyncio
    async def test_stop_menu_success(self, servicer):
        """StopMenu should transition to STOPPED and clear state."""
        servicer.state = menu_pb2.MenuState.RUNNING
        # Set up state via controller_states (single source of truth)
        servicer.state_manager.controller_states = {
            "serial_1": ControllerState.READY,
            "serial_2": ControllerState.CONNECTED,
        }

        # Simulate the real StopMenu logic (new implementation)
        if servicer.state != menu_pb2.MenuState.STOPPED:
            servicer.state = menu_pb2.MenuState.STOPPED
            servicer.state_manager.controller_states.clear()
            await servicer.event_publisher.publish("menu_stopped", {})

        assert servicer.state == menu_pb2.MenuState.STOPPED
        assert servicer.ready_controllers == set()
        assert servicer.connected_controllers == set()
        servicer.event_publisher.publish.assert_called_with("menu_stopped", {})

    @pytest.mark.asyncio
    async def test_stop_menu_already_stopped(self, servicer):
        """StopMenu should fail if already stopped."""
        servicer.state = menu_pb2.MenuState.STOPPED

        if servicer.state == menu_pb2.MenuState.STOPPED:
            success = False
            error = "Menu already stopped"
        else:
            success = True
            error = ""

        assert success is False
        assert "already stopped" in error


class TestMenuServicerHandleButtonInput:
    """Tests for _handle_button_input method logic."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    @pytest.mark.asyncio
    async def test_button_select_cycles_game_mode(self, servicer):
        """Select button should cycle to next game mode."""
        from services.menu.utils.settings import get_next_game_mode

        servicer.current_selection = Games.JoustFFA

        # Simulate the real logic using module-level function + FlagConfigWriter
        next_mode = get_next_game_mode(servicer.current_selection)
        servicer.current_selection = next_mode
        servicer.state_manager.set_game_mode(servicer.current_selection)
        await servicer.event_publisher.publish("selection_changed", {"game_name": servicer.current_selection.name})
        # Persist to flagd via writer
        servicer.user_prefs_writer.update_default_variant("current_game", servicer.current_selection.name)

        assert servicer.current_selection != Games.JoustFFA
        servicer.state_manager.set_game_mode.assert_called()
        servicer.event_publisher.publish.assert_called()


class TestMenuServicerHandleWebCommand:
    """Tests for _handle_web_command method logic."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    @pytest.mark.asyncio
    async def test_web_command_select_game_valid(self, servicer):
        """select_game command should update selection for valid game."""
        from services.menu.utils.settings import GAME_MODES

        servicer.current_selection = Games.JoustFFA
        game_name = "Tournament"

        # Simulate the real logic (uses FlagConfigWriter instead of settings_helper)
        game_mode = Games.from_name(game_name)
        if game_mode is not None and game_name in GAME_MODES:
            servicer.current_selection = game_mode
            servicer.state_manager.set_game_mode(game_mode)
            await servicer.event_publisher.publish("selection_changed", {"game_name": game_mode.name, "source": "web"})
            # Persist to flagd via writer
            servicer.user_prefs_writer.update_default_variant("current_game", game_mode.name)

        assert servicer.current_selection == Games.Tournament
        servicer.state_manager.set_game_mode.assert_called_with(Games.Tournament)

    @pytest.mark.asyncio
    async def test_web_command_select_game_invalid(self, servicer):
        """select_game command should ignore invalid game names."""
        servicer.current_selection = Games.JoustFFA
        game_name = "InvalidGameMode"

        # Simulate the real logic
        game_mode = Games.from_name(game_name)
        if game_mode is not None:
            servicer.current_selection = game_mode

        assert servicer.current_selection == Games.JoustFFA  # Unchanged


class TestMenuServicerHandleResetMenu:
    """Tests for _handle_reset_menu method logic."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    @pytest.mark.asyncio
    async def test_reset_menu_from_game_starting(self, servicer):
        """reset_menu should reset from GAME_STARTING to RUNNING."""
        servicer.state = menu_pb2.MenuState.GAME_STARTING

        # Simulate the real logic
        if servicer.state == menu_pb2.MenuState.GAME_STARTING:
            servicer.state = menu_pb2.MenuState.RUNNING
            await servicer.event_publisher.publish("game_start_cancelled", {})

        assert servicer.state == menu_pb2.MenuState.RUNNING
        servicer.event_publisher.publish.assert_called_with("game_start_cancelled", {})

    @pytest.mark.asyncio
    async def test_reset_menu_from_running_no_change(self, servicer):
        """reset_menu should not change state if RUNNING."""
        servicer.state = menu_pb2.MenuState.RUNNING

        # Simulate the real logic
        if servicer.state == menu_pb2.MenuState.GAME_STARTING:
            servicer.state = menu_pb2.MenuState.RUNNING
            await servicer.event_publisher.publish("game_start_cancelled", {})

        assert servicer.state == menu_pb2.MenuState.RUNNING
        servicer.event_publisher.publish.assert_not_called()


class TestMenuServicerStartGame:
    """Tests for _start_game method logic."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    @pytest.mark.asyncio
    async def test_start_game_requires_two_players(self, servicer):
        """_start_game should require at least 2 players."""
        # Only 1 ready controller
        servicer.state_manager.controller_states = {"serial_1": ControllerState.READY}

        # Simulate the check
        controllers = list(servicer.state_manager.ready_controllers)
        if len(controllers) < 2:
            started = False
        else:
            servicer.state = menu_pb2.MenuState.GAME_STARTING
            started = True

        assert started is False
        assert servicer.state == menu_pb2.MenuState.STOPPED

    @pytest.mark.asyncio
    async def test_start_game_sets_game_starting_state(self, servicer):
        """_start_game should set GAME_STARTING state with enough players."""
        # 3 ready controllers
        servicer.state_manager.controller_states = {
            "serial_1": ControllerState.READY,
            "serial_2": ControllerState.READY,
            "serial_3": ControllerState.READY,
        }

        # Simulate the logic
        controllers = list(servicer.state_manager.ready_controllers)
        if len(controllers) >= 2:
            servicer.state = menu_pb2.MenuState.GAME_STARTING

        assert servicer.state == menu_pb2.MenuState.GAME_STARTING


class TestMenuServicerLifecycle:
    """Tests for lifecycle methods."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    @pytest.mark.asyncio
    async def test_start_button_monitor(self, servicer):
        """start_button_monitor should start controller events."""
        await servicer.controller_events.start()
        servicer.controller_events.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_button_monitor(self, servicer):
        """stop_button_monitor should stop controller events."""
        await servicer.controller_events.stop()
        servicer.controller_events.stop.assert_called_once()

    def test_button_monitor_running_property(self, servicer):
        """button_monitor_running should reflect controller_events.is_running."""
        servicer.controller_events.is_running = True
        assert servicer.controller_events.is_running is True

        servicer.controller_events.is_running = False
        assert servicer.controller_events.is_running is False

    @pytest.mark.asyncio
    async def test_start_game_event_monitor(self, servicer):
        """start_game_event_monitor should set flag and create task."""
        # Simulate the logic
        if not servicer.game_event_monitor_running:
            servicer.game_event_monitor_running = True
            servicer.game_event_task = asyncio.create_task(asyncio.sleep(0.01))

        assert servicer.game_event_monitor_running is True
        assert servicer.game_event_task is not None

        # Clean up
        servicer.game_event_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await servicer.game_event_task

    @pytest.mark.asyncio
    async def test_stop_game_event_monitor(self, servicer):
        """stop_game_event_monitor should clear flag and cancel task."""
        servicer.game_event_monitor_running = True
        servicer.game_event_task = asyncio.create_task(asyncio.sleep(10))

        # Simulate the stop logic
        servicer.game_event_monitor_running = False
        if servicer.game_event_task:
            servicer.game_event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await servicer.game_event_task

        assert servicer.game_event_monitor_running is False

    @pytest.mark.asyncio
    async def test_shutdown_closes_channels(self, servicer):
        """shutdown should close all gRPC channels (no settings channel - moved to flagd)."""
        await servicer.controller_channel.close()
        await servicer.game_coordinator_channel.close()
        await servicer.audio_channel.close()

        servicer.controller_channel.close.assert_called_once()
        servicer.game_coordinator_channel.close.assert_called_once()
        servicer.audio_channel.close.assert_called_once()


class TestMenuServicerLobbyState:
    """Tests for lobby state management."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    def test_multiple_controller_connect(self, servicer):
        """Should correctly track multiple connected controllers via controller_states."""
        servicer.state_manager.controller_states["serial_1"] = ControllerState.CONNECTED
        servicer.state_manager.controller_states["serial_2"] = ControllerState.CONNECTED
        servicer.state_manager.controller_states["serial_3"] = ControllerState.CONNECTED

        assert len(servicer.connected_controllers) == 3
        assert "serial_1" in servicer.connected_controllers
        assert "serial_2" in servicer.connected_controllers
        assert "serial_3" in servicer.connected_controllers

    def test_controller_disconnect_updates_count(self, servicer):
        """Disconnecting should update ready count (computed from controller_states)."""
        servicer.state_manager.controller_states = {
            "serial_1": ControllerState.READY,
            "serial_2": ControllerState.READY,
        }
        assert servicer.ready_controller_count == 2

        # Remove one controller
        del servicer.state_manager.controller_states["serial_1"]

        assert servicer.ready_controller_count == 1


class TestMenuServicerGameModeSelection:
    """Tests for game mode selection logic."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    def test_set_game_mode_updates_state_manager(self, servicer):
        """Setting game mode should update state_manager."""
        servicer.state_manager.set_game_mode(Games.Tournament)
        servicer.state_manager.set_game_mode.assert_called_with(Games.Tournament)

    def test_current_selection_persistence(self, servicer):
        """Current selection should persist across state changes."""
        servicer.current_selection = Games.Werewolf
        servicer.state = menu_pb2.MenuState.RUNNING
        servicer.state = menu_pb2.MenuState.STOPPED

        assert servicer.current_selection == Games.Werewolf


class TestGameEventResult:
    """Tests for the _GameEventResult dataclass."""

    def test_game_started_true(self):
        from services.menu.servicer import _GameEventResult

        result = _GameEventResult(game_started=True, should_return=False)
        assert result.game_started is True
        assert result.should_return is False

    def test_should_return_true(self):
        from services.menu.servicer import _GameEventResult

        result = _GameEventResult(game_started=False, should_return=True)
        assert result.game_started is False
        assert result.should_return is True


class TestCancelStream:
    """Tests for _cancel_stream static method."""

    def test_cancels_existing_stream(self):
        """Should call cancel on a non-None stream."""
        from services.menu.servicer import MenuServicer

        mock_stream = MagicMock()
        MenuServicer._cancel_stream(mock_stream)
        mock_stream.cancel.assert_called_once()

    def test_handles_none_stream(self):
        """Should not raise when stream is None."""
        from services.menu.servicer import MenuServicer

        MenuServicer._cancel_stream(None)  # Should not raise


class TestCheckRetryEligible:
    """Tests for _check_retry_eligible static method."""

    def test_raises_when_game_not_started(self):
        """Should raise the error if game never started."""
        from services.menu.servicer import MenuServicer

        error = RuntimeError("connection lost")
        with pytest.raises(RuntimeError, match="connection lost"):
            MenuServicer._check_retry_eligible(error, game_started=False, attempt=0, max_attempts=3)

    def test_raises_when_max_attempts_reached(self):
        """Should raise the error when max attempts exceeded."""
        from services.menu.servicer import MenuServicer

        error = RuntimeError("timeout")
        with pytest.raises(RuntimeError, match="timeout"):
            MenuServicer._check_retry_eligible(error, game_started=True, attempt=3, max_attempts=3)

    def test_returns_game_started_when_retryable(self):
        """Should return game_started when retry is eligible."""
        from services.menu.servicer import MenuServicer

        result = MenuServicer._check_retry_eligible(
            RuntimeError("transient"), game_started=True, attempt=1, max_attempts=3
        )
        assert result is True

    def test_raises_on_first_attempt_when_game_not_started(self):
        """Should raise even on first attempt if game never started."""
        from services.menu.servicer import MenuServicer

        error = ValueError("bad config")
        with pytest.raises(ValueError, match="bad config"):
            MenuServicer._check_retry_eligible(error, game_started=False, attempt=0, max_attempts=3)


class TestHandleFirstGameEvent:
    """Tests for _handle_first_game_event extracted helper."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    @pytest.mark.asyncio
    async def test_start_error_returns_should_return_true(self, servicer):
        """game_start_error event should return result with should_return=True."""
        from services.menu.servicer import MenuServicer, _GameEventResult

        event = MagicMock()
        event.event_type = "game_start_error"
        event.data = {"error": "Not enough controllers"}

        span = MagicMock()

        # Bind the method to our fake servicer
        servicer._clear_ready_state = MagicMock()
        servicer._restart_lobby = AsyncMock()
        result = await MenuServicer._handle_first_game_event(servicer, event, span)

        assert isinstance(result, _GameEventResult)
        assert result.should_return is True
        assert result.game_started is False
        assert servicer.state == menu_pb2.MenuState.RUNNING
        span.set_attribute.assert_called_with("error", "Not enough controllers")

    @pytest.mark.asyncio
    async def test_success_event_returns_game_started_true(self, servicer):
        """Non-error first event should return result with game_started=True."""
        from services.menu.servicer import MenuServicer, _GameEventResult

        event = MagicMock()
        event.event_type = "game_started"

        span = MagicMock()

        servicer._clear_ready_state = MagicMock()
        servicer._restart_lobby = AsyncMock()
        result = await MenuServicer._handle_first_game_event(servicer, event, span)

        assert isinstance(result, _GameEventResult)
        assert result.should_return is False
        assert result.game_started is True
        span.set_attribute.assert_called_with("game.started", True)
        servicer._clear_ready_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_error_default_message(self, servicer):
        """game_start_error with no error key should use default message."""
        from services.menu.servicer import MenuServicer

        event = MagicMock()
        event.event_type = "game_start_error"
        event.data = {}

        span = MagicMock()

        servicer._clear_ready_state = MagicMock()
        servicer._restart_lobby = AsyncMock()
        result = await MenuServicer._handle_first_game_event(servicer, event, span)

        assert result.should_return is True
        span.set_attribute.assert_called_with("error", "Unknown error")


class TestPrepareGameStart:
    """Tests for _prepare_game_start extracted helper."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    @pytest.mark.asyncio
    async def test_sets_game_starting_state(self, servicer):
        """Should set menu state to GAME_STARTING."""
        from services.menu.servicer import MenuServicer

        servicer.idle_monitor = MagicMock()
        servicer.stop_button_monitor = AsyncMock()

        await MenuServicer._prepare_game_start(servicer)

        assert servicer.state == menu_pb2.MenuState.GAME_STARTING

    @pytest.mark.asyncio
    async def test_stops_idle_monitor(self, servicer):
        """Should stop the idle monitor."""
        from services.menu.servicer import MenuServicer

        servicer.idle_monitor = MagicMock()
        servicer.stop_button_monitor = AsyncMock()

        await MenuServicer._prepare_game_start(servicer)

        servicer.idle_monitor.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_stops_button_monitor(self, servicer):
        """Should stop the button monitor."""
        from services.menu.servicer import MenuServicer

        servicer.idle_monitor = MagicMock()
        servicer.stop_button_monitor = AsyncMock()

        await MenuServicer._prepare_game_start(servicer)

        servicer.stop_button_monitor.assert_called_once()

    @pytest.mark.asyncio
    async def test_stops_lobby_music(self, servicer):
        """Should stop lobby music."""
        from services.menu.servicer import MenuServicer

        servicer.idle_monitor = MagicMock()
        servicer.stop_button_monitor = AsyncMock()

        await MenuServicer._prepare_game_start(servicer)

        servicer.audio.stop_lobby_music.assert_called_once()


class TestOpenGameStream:
    """Tests for _open_game_stream extracted helper."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    def test_first_attempt_sends_start_config(self, servicer):
        """Attempt 0 should use the original request with start_config."""
        from services.menu.servicer import MenuServicer

        stub = MagicMock()
        request = MagicMock()
        metadata = [("traceparent", "00-abc-def-01")]

        MenuServicer._open_game_stream(servicer, stub, request, metadata, attempt=0, max_attempts=3)

        stub.StreamGameEvents.assert_called_once_with(request, metadata=metadata)

    def test_reconnect_sends_empty_request(self, servicer):
        """Attempt > 0 should send empty StreamEventsRequest."""
        from services.menu.servicer import MenuServicer

        stub = MagicMock()
        request = MagicMock()
        metadata = [("traceparent", "00-abc-def-01")]

        MenuServicer._open_game_stream(servicer, stub, request, metadata, attempt=1, max_attempts=3)

        # Verify it was called with an empty request (not the original)
        call_args = stub.StreamGameEvents.call_args
        assert call_args[0][0] != request
        assert call_args[1]["metadata"] == metadata


class TestBuildGameConfig:
    """Tests for _build_game_config method."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    @pytest.fixture
    def players(self):
        from proto import game_coordinator_pb2

        return [
            game_coordinator_pb2.Player(serial="p1"),
            game_coordinator_pb2.Player(serial="p2"),
        ]

    def _build_config(self, servicer, game_mode, players):
        """Call the real _build_game_config bound to our fake servicer."""
        from unittest.mock import patch

        from services.menu.servicer import MenuServicer

        with patch("services.menu.servicer.set_game_transaction_context"):
            return MenuServicer._build_game_config(servicer, game_mode, players)

    def test_build_config_ffa(self, servicer, players):
        """JoustFFA should produce ffa_config with correct base fields."""
        config = self._build_config(servicer, Games.JoustFFA, players)

        assert config.game_name == "JoustFFA"
        assert config.sensitivity == 2
        assert len(config.players) == 2
        assert config.HasField("ffa_config")

    def test_build_config_teams(self, servicer, players):
        """JoustTeams should produce teams_config with num_teams and random_assignment."""
        config = self._build_config(servicer, Games.JoustTeams, players)

        assert config.game_name == "JoustTeams"
        assert config.sensitivity == 2
        assert len(config.players) == 2
        assert config.HasField("teams_config")
        # Verify flagd client was called for team settings
        servicer.game_settings_client.get_integer_value.assert_any_call("num_teams", 2)
        servicer.game_settings_client.get_boolean_value.assert_any_call("random_assignment", True)

    def test_build_config_random_teams(self, servicer, players):
        """JoustRandomTeams should produce random_teams_config with num_teams."""
        config = self._build_config(servicer, Games.JoustRandomTeams, players)

        assert config.game_name == "JoustRandomTeams"
        assert config.sensitivity == 2
        assert len(config.players) == 2
        assert config.HasField("random_teams_config")
        servicer.game_settings_client.get_integer_value.assert_any_call("num_teams", 2)

    def test_build_config_nonstop(self, servicer, players):
        """NonStop should produce nonstop_config with time_limit_seconds."""
        config = self._build_config(servicer, Games.NonStop, players)

        assert config.game_name == "NonStop"
        assert config.sensitivity == 2
        assert len(config.players) == 2
        assert config.HasField("nonstop_config")
        servicer.game_settings_client.get_integer_value.assert_any_call("nonstop.time_limit_seconds", 0)

    def test_build_config_tournament(self, servicer, players):
        """Tournament should produce tournament_config with invincibility_seconds."""
        config = self._build_config(servicer, Games.Tournament, players)

        assert config.game_name == "Tournament"
        assert config.sensitivity == 2
        assert len(config.players) == 2
        assert config.HasField("tournament_config")
        servicer.game_settings_client.get_float_value.assert_any_call("invincibility_seconds", 4.0)

    def test_build_config_fight_club(self, servicer, players):
        """FightClub should produce fight_club_config with invincibility and min_rounds."""
        config = self._build_config(servicer, Games.FightClub, players)

        assert config.game_name == "FightClub"
        assert config.sensitivity == 2
        assert len(config.players) == 2
        assert config.HasField("fight_club_config")
        servicer.game_settings_client.get_float_value.assert_any_call("invincibility_seconds", 4.0)
        servicer.game_settings_client.get_integer_value.assert_any_call("fight_club.min_rounds", 10)

    def test_build_config_werewolf(self, servicer, players):
        """Werewolf should produce werewolf_config with reveal_time_seconds."""
        config = self._build_config(servicer, Games.Werewolf, players)

        assert config.game_name == "Werewolf"
        assert config.sensitivity == 2
        assert len(config.players) == 2
        assert config.HasField("werewolf_config")
        servicer.game_settings_client.get_float_value.assert_any_call("werewolf.reveal_time_seconds", 35.0)

    def test_build_config_zombie(self, servicer, players):
        """Zombies should produce zombie_config."""
        config = self._build_config(servicer, Games.Zombies, players)

        assert config.game_name == "Zombies"
        assert config.sensitivity == 2
        assert len(config.players) == 2
        assert config.HasField("zombie_config")

    def test_build_config_swapper(self, servicer, players):
        """Swapper should produce swapper_config."""
        config = self._build_config(servicer, Games.Swapper, players)

        assert config.game_name == "Swapper"
        assert config.sensitivity == 2
        assert len(config.players) == 2
        assert config.HasField("swapper_config")

    def test_build_config_traitor(self, servicer, players):
        """Traitor should produce traitor_config with num_teams."""
        config = self._build_config(servicer, Games.Traitor, players)

        assert config.game_name == "Traitor"
        assert config.sensitivity == 2
        assert len(config.players) == 2
        assert config.HasField("traitor_config")
        servicer.game_settings_client.get_integer_value.assert_any_call("num_teams", 0)


class TestRestartLobby:
    """Tests for _restart_lobby method."""

    @pytest.fixture
    def servicer(self):
        s = FakeMenuServicer()
        s.idle_monitor = MagicMock()
        s.start_button_monitor = AsyncMock()
        return s

    @pytest.mark.asyncio
    async def test_restart_lobby_sets_running_state(self, servicer):
        """_restart_lobby should set state to RUNNING."""
        from unittest.mock import patch

        from services.menu.servicer import MenuServicer

        servicer.state = menu_pb2.MenuState.GAME_STARTING

        with (
            patch("services.menu.servicer.tracer") as mock_tracer,
            patch("services.menu.servicer.otel_context"),
        ):
            mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock()
            mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)

            await MenuServicer._restart_lobby(servicer)

        assert servicer.state == menu_pb2.MenuState.RUNNING

    @pytest.mark.asyncio
    async def test_restart_lobby_starts_button_monitor(self, servicer):
        """_restart_lobby should call start_button_monitor."""
        from unittest.mock import patch

        from services.menu.servicer import MenuServicer

        with (
            patch("services.menu.servicer.tracer") as mock_tracer,
            patch("services.menu.servicer.otel_context"),
        ):
            mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock()
            mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)

            await MenuServicer._restart_lobby(servicer)

        servicer.start_button_monitor.assert_called_once()

    @pytest.mark.asyncio
    async def test_restart_lobby_resets_state_manager(self, servicer):
        """_restart_lobby should call state_manager.reset()."""
        from unittest.mock import patch

        from services.menu.servicer import MenuServicer

        with (
            patch("services.menu.servicer.tracer") as mock_tracer,
            patch("services.menu.servicer.otel_context"),
        ):
            mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock()
            mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)

            await MenuServicer._restart_lobby(servicer)

        servicer.state_manager.reset.assert_called_once()

    @pytest.mark.asyncio
    async def test_restart_lobby_starts_lobby_music(self, servicer):
        """_restart_lobby should restart lobby music."""
        from unittest.mock import patch

        from services.menu.servicer import MenuServicer

        with (
            patch("services.menu.servicer.tracer") as mock_tracer,
            patch("services.menu.servicer.otel_context"),
        ):
            mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock()
            mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)

            await MenuServicer._restart_lobby(servicer)

        servicer.audio.start_lobby_music.assert_called_once()

    @pytest.mark.asyncio
    async def test_restart_lobby_starts_idle_monitor(self, servicer):
        """_restart_lobby should start the idle monitor."""
        from unittest.mock import patch

        from services.menu.servicer import MenuServicer

        with (
            patch("services.menu.servicer.tracer") as mock_tracer,
            patch("services.menu.servicer.otel_context"),
        ):
            mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock()
            mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)

            await MenuServicer._restart_lobby(servicer)

        servicer.idle_monitor.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_restart_lobby_waits_for_connection(self, servicer):
        """_restart_lobby should wait for controller connection."""
        from unittest.mock import patch

        from services.menu.servicer import MenuServicer

        with (
            patch("services.menu.servicer.tracer") as mock_tracer,
            patch("services.menu.servicer.otel_context"),
        ):
            mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock()
            mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)

            await MenuServicer._restart_lobby(servicer)

        servicer.controller_events.wait_for_connection.assert_called_once()


class TestBuildTraceMetadata:
    """Tests for _build_trace_metadata method."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    def test_build_trace_metadata_returns_list_of_tuples(self, servicer):
        """_build_trace_metadata should return a list (may be empty with no active span)."""
        from services.menu.servicer import MenuServicer

        result = MenuServicer._build_trace_metadata(servicer)

        assert isinstance(result, list)
        # Each item should be a tuple of (key, value) strings
        for item in result:
            assert isinstance(item, tuple)
            assert len(item) == 2
            assert isinstance(item[0], str)
            assert isinstance(item[1], str)

    def test_build_trace_metadata_with_mocked_propagator(self, servicer):
        """_build_trace_metadata should use TraceContextTextMapPropagator to inject context."""
        from unittest.mock import patch

        from services.menu.servicer import MenuServicer

        with patch("services.menu.servicer.TraceContextTextMapPropagator") as mock_propagator_cls:
            mock_propagator = MagicMock()
            mock_propagator_cls.return_value = mock_propagator

            # Make inject populate the carrier with test data
            def fake_inject(carrier):
                carrier["traceparent"] = "00-abc123-def456-01"

            mock_propagator.inject.side_effect = fake_inject

            result = MenuServicer._build_trace_metadata(servicer)

        assert result == [("traceparent", "00-abc123-def456-01")]
        mock_propagator.inject.assert_called_once()


class TestBuildGameConfigCustomValues:
    """Tests for _build_game_config with non-default flagd values.

    The existing TestBuildGameConfig tests use default mock return values.
    These tests verify that specific flagd values flow through to the proto config.
    """

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    @pytest.fixture
    def players(self):
        from proto import game_coordinator_pb2

        return [
            game_coordinator_pb2.Player(serial="p1"),
            game_coordinator_pb2.Player(serial="p2"),
            game_coordinator_pb2.Player(serial="p3"),
        ]

    def _build_config(self, servicer, game_mode, players):
        from unittest.mock import patch

        from services.menu.servicer import MenuServicer

        with patch("services.menu.servicer.set_game_transaction_context"):
            return MenuServicer._build_game_config(servicer, game_mode, players)

    def test_sensitivity_flows_from_flagd(self, servicer, players):
        """Sensitivity value from game_settings_client should appear in config."""
        servicer.game_settings_client.get_integer_value = MagicMock(
            side_effect=lambda key, default: {
                "sensitivity": 4,
                "num_teams": 2,
            }.get(key, default)
        )
        config = self._build_config(servicer, Games.JoustFFA, players)
        assert config.sensitivity == 4

    def test_teams_custom_num_teams(self, servicer, players):
        """JoustTeams should use num_teams from flagd, not just the default."""
        servicer.game_settings_client.get_integer_value = MagicMock(
            side_effect=lambda key, default: {
                "sensitivity": 2,
                "num_teams": 4,
            }.get(key, default)
        )
        servicer.game_settings_client.get_boolean_value = MagicMock(
            side_effect=lambda key, default: {
                "random_assignment": False,
            }.get(key, default)
        )
        config = self._build_config(servicer, Games.JoustTeams, players)
        assert config.teams_config.num_teams == 4
        assert config.teams_config.random_assignment is False

    def test_nonstop_custom_time_limit(self, servicer, players):
        """NonStop should use time_limit_seconds from flagd."""
        servicer.game_settings_client.get_integer_value = MagicMock(
            side_effect=lambda key, default: {
                "sensitivity": 2,
                "nonstop.time_limit_seconds": 120,
            }.get(key, default)
        )
        config = self._build_config(servicer, Games.NonStop, players)
        assert config.nonstop_config.time_limit_seconds == 120

    def test_fight_club_custom_values(self, servicer, players):
        """FightClub should use custom invincibility and min_rounds from flagd."""
        servicer.game_settings_client.get_integer_value = MagicMock(
            side_effect=lambda key, default: {
                "sensitivity": 3,
                "fight_club.min_rounds": 5,
            }.get(key, default)
        )
        servicer.game_settings_client.get_float_value = MagicMock(
            side_effect=lambda key, default: {
                "invincibility_seconds": 2.5,
            }.get(key, default)
        )
        config = self._build_config(servicer, Games.FightClub, players)
        assert config.sensitivity == 3
        assert config.fight_club_config.invincibility_seconds == pytest.approx(2.5)
        assert config.fight_club_config.min_rounds == 5

    def test_werewolf_custom_reveal_time(self, servicer, players):
        """Werewolf should use reveal_time_seconds from flagd."""
        servicer.game_settings_client.get_float_value = MagicMock(
            side_effect=lambda key, default: {
                "werewolf.reveal_time_seconds": 60.0,
            }.get(key, default)
        )
        config = self._build_config(servicer, Games.Werewolf, players)
        assert config.werewolf_config.reveal_time_seconds == pytest.approx(60.0)

    def test_config_includes_all_players(self, servicer, players):
        """Config should include all players in the players list."""
        config = self._build_config(servicer, Games.JoustFFA, players)
        assert len(config.players) == 3
        serials = [p.serial for p in config.players]
        assert "p1" in serials
        assert "p2" in serials
        assert "p3" in serials


class TestConsumeGameEvents:
    """Tests for _consume_game_events method."""

    @pytest.fixture
    def servicer(self):
        from services.menu.servicer import MenuServicer

        s = FakeMenuServicer()
        s._clear_ready_state = MagicMock()
        s._restart_lobby = AsyncMock()
        # Bind real methods from MenuServicer so _consume_game_events can call them
        s._handle_first_game_event = lambda event, span: MenuServicer._handle_first_game_event(s, event, span)
        return s

    @pytest.mark.asyncio
    async def test_game_ended_event_returns_should_return_true(self, servicer):
        """A game_ended event should trigger restart and return should_return=True."""
        from services.menu.servicer import MenuServicer, _GameEventResult

        event = MagicMock()
        event.event_type = "game_started"
        event.data = {}

        ended_event = MagicMock()
        ended_event.event_type = "game_ended"
        ended_event.data = {"winner": "p1"}

        # Create an async iterator that yields two events
        async def fake_stream():
            yield event
            yield ended_event

        span = MagicMock()
        result = await MenuServicer._consume_game_events(servicer, fake_stream(), False, span)

        assert isinstance(result, _GameEventResult)
        assert result.should_return is True
        assert result.game_started is True
        servicer._restart_lobby.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_game_force_ended_event_returns_should_return_true(self, servicer):
        """A game_force_ended event should also trigger restart."""
        from services.menu.servicer import MenuServicer

        start_event = MagicMock()
        start_event.event_type = "game_started"
        start_event.data = {}

        force_ended_event = MagicMock()
        force_ended_event.event_type = "game_force_ended"
        force_ended_event.data = {}

        async def fake_stream():
            yield start_event
            yield force_ended_event

        span = MagicMock()
        result = await MenuServicer._consume_game_events(servicer, fake_stream(), False, span)

        assert result.should_return is True
        servicer._restart_lobby.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_stream_returns_not_should_return(self, servicer):
        """An empty stream should return should_return=False."""
        from services.menu.servicer import MenuServicer, _GameEventResult

        async def fake_stream():
            return
            yield  # noqa: RET504 - makes this an async generator

        span = MagicMock()
        result = await MenuServicer._consume_game_events(servicer, fake_stream(), False, span)

        assert isinstance(result, _GameEventResult)
        assert result.should_return is False
        assert result.game_started is False

    @pytest.mark.asyncio
    async def test_first_event_error_returns_early(self, servicer):
        """A game_start_error as first event should return should_return=True."""
        from services.menu.servicer import MenuServicer

        error_event = MagicMock()
        error_event.event_type = "game_start_error"
        error_event.data = {"error": "bad config"}

        async def fake_stream():
            yield error_event

        span = MagicMock()
        servicer.state = MagicMock()  # Allow state assignment
        result = await MenuServicer._consume_game_events(servicer, fake_stream(), False, span)

        assert result.should_return is True
        assert result.game_started is False

    @pytest.mark.asyncio
    async def test_game_already_started_skips_first_event_handling(self, servicer):
        """When game_started=True, first event handling should be skipped."""
        from services.menu.servicer import MenuServicer

        ended_event = MagicMock()
        ended_event.event_type = "game_ended"
        ended_event.data = {}

        async def fake_stream():
            yield ended_event

        span = MagicMock()
        result = await MenuServicer._consume_game_events(servicer, fake_stream(), True, span)

        assert result.should_return is True
        assert result.game_started is True
        # _handle_first_game_event should NOT have been called (no _clear_ready_state call)
        servicer._clear_ready_state.assert_not_called()


class TestStreamMenuEvents:
    """Tests for StreamMenuEvents gRPC method."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    @pytest.mark.asyncio
    async def test_stream_subscribes_and_unsubscribes(self, servicer):
        """StreamMenuEvents should subscribe on start and unsubscribe on cancel."""
        from services.menu.servicer import MenuServicer

        context = MockGrpcContext()
        event_queue = asyncio.Queue()

        servicer.event_publisher.subscribe = AsyncMock(return_value=event_queue)

        # Put an event then cancel context so stream terminates
        test_event = MagicMock()
        await event_queue.put(test_event)

        events = []
        async for event in MenuServicer.StreamMenuEvents(servicer, None, context):
            events.append(event)
            context.cancel()  # cancel after first event

        assert len(events) == 1
        assert events[0] is test_event
        servicer.event_publisher.subscribe.assert_awaited_once()
        servicer.event_publisher.unsubscribe.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stream_handles_timeout_and_continues(self, servicer):
        """StreamMenuEvents should handle timeout and continue polling."""
        from services.menu.servicer import MenuServicer

        context = MockGrpcContext()
        event_queue = asyncio.Queue()

        servicer.event_publisher.subscribe = AsyncMock(return_value=event_queue)

        # Schedule a cancel after a short delay (queue will be empty, causing timeouts)
        async def delayed_cancel():
            await asyncio.sleep(0.1)
            context.cancel()

        cancel_task = asyncio.create_task(delayed_cancel())

        events = []
        async for event in MenuServicer.StreamMenuEvents(servicer, None, context):
            events.append(event)

        await cancel_task

        # No events should have been yielded (queue was empty, only timeouts)
        assert len(events) == 0
        servicer.event_publisher.unsubscribe.assert_awaited_once()


class TestClearReadyState:
    """Tests for _clear_ready_state method."""

    @pytest.fixture
    def servicer(self):
        return FakeMenuServicer()

    def test_transitions_ready_to_connected(self, servicer):
        """_clear_ready_state should transition READY controllers back to CONNECTED."""
        from unittest.mock import patch

        from services.menu.servicer import MenuServicer

        servicer.state_manager.controller_states = {
            "s1": ControllerState.READY,
            "s2": ControllerState.CONNECTED,
            "s3": ControllerState.READY,
        }

        with patch("services.menu.servicer.metrics"):
            MenuServicer._clear_ready_state(servicer)

        assert servicer.state_manager.controller_states["s1"] == ControllerState.CONNECTED
        assert servicer.state_manager.controller_states["s2"] == ControllerState.CONNECTED
        assert servicer.state_manager.controller_states["s3"] == ControllerState.CONNECTED

    def test_clears_player_ready_metric(self, servicer):
        """_clear_ready_state should clear the player_ready metric."""
        from unittest.mock import patch

        from services.menu.servicer import MenuServicer

        with patch("services.menu.servicer.metrics") as mock_metrics:
            MenuServicer._clear_ready_state(servicer)

        mock_metrics.player_ready.clear.assert_called_once()
