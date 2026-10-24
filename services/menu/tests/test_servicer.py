"""
Unit tests for MenuServicer.

Tests the core Menu gRPC service:
- State management
- Controller event callbacks
- Admin combo detection
- ProcessInput (button_press / web_command / reset_menu)
- Ready-up and game start (UC-010)
- Lifecycle methods

Behaviour tests use the ``real_servicer`` fixture: a real MenuServicer with its
real StateManager and handlers, where only I/O edges are autospec'd (#1358).
Tests for extracted helpers still bind the real method to ``FakeMenuServicer``.

Issue #209: Improve test coverage for critical game flow
"""

import asyncio
import contextlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from lib.types import Games
from proto import controller_manager_pb2, menu_pb2
from services.menu.handlers.base import ControllerState
from services.menu.tests.servicer_harness import default_returning_flag_client
from services.menu.utils.led import GAME_MODE_COLORS


class MockGrpcContext:
    """Mock gRPC context."""

    def __init__(self):
        self._cancelled = False

    def cancelled(self):
        return self._cancelled

    def cancel(self):
        self._cancelled = True

    async def send_initial_metadata(self, _metadata):
        """The stream servicers send headers once subscribed (#1613)."""
        self.initial_metadata_sent = True


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

        # Mocked dependencies
        self.led = MagicMock()
        self.audio = MagicMock()
        self.audio.start_lobby_music = AsyncMock()
        self.audio.stop_lobby_music = AsyncMock()
        self.audio.play_voice = AsyncMock()

        # FlagServiceWriter mocks (replacing SettingsHelper)
        self.game_settings_writer = MagicMock()
        self.game_settings_writer.update_default_variant = MagicMock(return_value=True)

        self.user_prefs_writer = MagicMock()
        self.user_prefs_writer.update_default_variant = MagicMock(return_value=True)

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
        self.state_manager.reset = AsyncMock(return_value=["serial_1", "serial_2"])
        # controller_states is the single source of truth (starts empty)
        self.state_manager.controller_states = {}

        # Computed properties on state_manager (match real implementation)
        type(self.state_manager).ready_controllers = property(
            lambda sm: {s for s, st in sm.controller_states.items() if st == ControllerState.READY}
        )
        type(self.state_manager).connected_controllers = property(lambda sm: set(sm.controller_states.keys()))

        # _restart_lobby waits for the button stream to reconnect.
        self.controller_events = MagicMock()
        self.controller_events.wait_for_connection = AsyncMock()

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


def _published(servicer) -> list[tuple[str, dict]]:
    """(event_type, data) of every event the servicer published, in order."""
    return [c.args for c in servicer.event_publisher.publish.await_args_list]


async def _await_lifecycle(servicer) -> None:
    """Wait for the background game-lifecycle task the real _start_game scheduled."""
    assert servicer._game_lifecycle_task is not None, "no game start was scheduled"
    await servicer._game_lifecycle_task


def _started_roster(stream_mock) -> list[str]:
    """Serials in the start_config the lifecycle sent to the coordinator."""
    request = stream_mock.await_args.args[2]
    return sorted(p.serial for p in request.start_config.players)


async def _connect(servicer, *serials: str) -> None:
    for serial in serials:
        await servicer.on_connect(serial)


async def _ready_up(servicer, *serials: str) -> None:
    """Press the trigger on each controller (UC-010 steps 1-3)."""
    for serial in serials:
        await servicer.on_button(serial, "trigger", True)
        await servicer.on_button(serial, "trigger", False)


class TestMenuServicerState:
    """Initial state of a freshly constructed MenuServicer."""

    @pytest.mark.asyncio
    async def test_initial_state(self, real_servicer):
        """A new servicer is STOPPED, selects JoustFFA and has an empty lobby."""
        assert real_servicer.state == menu_pb2.MenuState.STOPPED
        assert real_servicer.current_selection == Games.JoustFFA
        assert real_servicer.connected_controllers == set()
        assert real_servicer.ready_controllers == set()
        assert real_servicer.ready_controller_count == 0

    @pytest.mark.asyncio
    async def test_game_options_are_the_playable_modes(self, real_servicer):
        """get_game_options returns a copy of GAME_MODES (UC-011 BR-001 source list)."""
        from services.menu.utils.settings import GAME_MODES

        options = real_servicer.get_game_options()
        assert options == GAME_MODES
        options.append("Bogus")
        assert "Bogus" not in real_servicer.get_game_options()

    @pytest.mark.asyncio
    async def test_set_and_get_menu_state(self, real_servicer):
        """set_menu_state / get_menu_state round-trip through the servicer."""
        real_servicer.set_menu_state(menu_pb2.MenuState.GAME_STARTING)
        assert real_servicer.get_menu_state() == menu_pb2.MenuState.GAME_STARTING


class TestMenuServicerControllerCallbacks:
    """ControllerEventCallbacks: connect, disconnect, battery, stream reconnect."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="main", steps=[6], cond=["Post-S-1"])
    async def test_on_connect_registers_connected_with_dim_led(self, real_servicer):
        """on_connect adds the serial as CONNECTED and lights the dim lobby colour."""
        await _connect(real_servicer, "s1", "s2")

        assert real_servicer.connected_controllers == {"s1", "s2"}
        assert real_servicer.state_manager.controller_states["s1"] == ControllerState.CONNECTED
        real_servicer.led.set_connected_color.assert_any_await("s1", Games.JoustFFA.name)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A1", cond=["Post-F-1"])
    async def test_on_disconnect_removes_ready_controller(self, real_servicer):
        """on_disconnect removes the serial from the lobby, including its ready flag."""
        await _connect(real_servicer, "s1", "s2", "s3")
        await _ready_up(real_servicer, "s1")
        assert real_servicer.ready_controller_count == 1

        await real_servicer.on_disconnect("s1")

        assert real_servicer.connected_controllers == {"s2", "s3"}
        assert real_servicer.ready_controllers == set()
        assert "s1" not in real_servicer.state_manager.button_states

    @pytest.mark.asyncio
    async def test_update_battery_is_tracked(self, real_servicer):
        """update_battery stores the level in the state manager."""
        real_servicer.update_battery("s1", 4)
        assert real_servicer.state_manager.battery_levels["s1"] == 4

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A5")
    async def test_stream_reconnect_clears_lobby(self, real_servicer):
        """A button-stream reconnect clears all lobby state, un-readying everyone."""
        await _connect(real_servicer, "s1", "s2", "s3")
        await _ready_up(real_servicer, "s1")

        await real_servicer.on_stream_reconnected()

        assert real_servicer.connected_controllers == set()
        assert real_servicer.ready_controllers == set()
        assert real_servicer.state_manager.button_states == {}
        assert real_servicer.state_manager.battery_levels == {}


class TestMenuServicerOnButton:
    """on_button: idle activity, admin combo detection and dispatch."""

    @pytest.mark.asyncio
    async def test_any_button_resets_idle_activity(self, real_servicer):
        """Every button event resets the idle timer."""
        await real_servicer.on_button("s1", "move", True)
        real_servicer.idle_monitor.reset_activity.assert_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[1, 2])
    async def test_four_face_buttons_enter_admin_mode(self, real_servicer):
        """UC-012 steps 1-2: Cross+Circle+Square+Triangle switches the controller to ADMIN.

        The admin enter effect is sent and the 60 s timeout task is started.
        """
        from proto import controller_manager_pb2

        real_servicer.state = menu_pb2.MenuState.RUNNING  # Pre-1
        await _connect(real_servicer, "host", "other")
        for button in ("cross", "circle", "square", "triangle"):
            await real_servicer.on_button("host", button, True)

        assert real_servicer.state_manager.controller_states["host"] == ControllerState.ADMIN
        assert real_servicer.state_manager.controller_states["other"] == ControllerState.CONNECTED
        assert real_servicer.admin_handler.is_admin_controller("host")
        real_servicer.led.send_game_effect.assert_any_await(
            "host", controller_manager_pb2.GAME_EFFECT_ADMIN_ENTER, color=None, duration_ms=0, speed=0
        )
        assert real_servicer.admin_handler._timeout_task is not None
        assert not real_servicer.admin_handler._timeout_task.done()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[1])
    async def test_partial_combo_stays_connected(self, real_servicer):
        """UC-012 step 1: three face buttons are not the combo; the controller stays CONNECTED."""
        real_servicer.state = menu_pb2.MenuState.RUNNING  # Pre-1, so only the partial combo keeps it out
        await _connect(real_servicer, "host")
        for button in ("cross", "circle", "square"):
            await real_servicer.on_button("host", button, True)

        assert real_servicer.state_manager.controller_states["host"] == ControllerState.CONNECTED
        assert not real_servicer.admin_handler.active

    @pytest.mark.asyncio
    async def test_face_button_release_rearms_combo(self, real_servicer):
        """Releasing a face button clears combo_shown so the next full combo is seen again."""
        real_servicer.state = menu_pb2.MenuState.RUNNING  # Pre-1
        await _connect(real_servicer, "host")
        for button in ("cross", "circle", "square", "triangle"):
            await real_servicer.on_button("host", button, True)
        assert real_servicer.admin_handler.combo_shown is True

        await real_servicer.on_button("host", "cross", False)

        assert real_servicer.admin_handler.combo_shown is False


class TestMenuServicerStartStopMenu:
    """StartMenu / StopMenu RPCs on the real servicer."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A11")
    async def test_start_menu_runs_lobby(self, real_servicer):
        """StartMenu sets RUNNING, un-readies everyone, starts music + idle monitor, publishes menu_started."""
        await _connect(real_servicer, "s1", "s2", "s3")
        await _ready_up(real_servicer, "s1")

        response = await real_servicer.StartMenu(menu_pb2.StartMenuRequest(), MagicMock())

        assert response.success is True
        assert real_servicer.state == menu_pb2.MenuState.RUNNING
        assert real_servicer.ready_controllers == set()
        real_servicer.audio.start_lobby_music.assert_awaited_once()
        real_servicer.idle_monitor.start.assert_called_once()
        assert ("menu_started", {}) in _published(real_servicer)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="main")  # Post-S-1: the next StartMenu restores current_game
    async def test_start_menu_restores_persisted_selection(self, real_servicer):
        """StartMenu restores the persisted current_game (UC-011 success postcondition)."""
        real_servicer.user_prefs_client.get_string_value.side_effect = lambda key, default, *_a, **_k: (
            "Tournament" if key == "current_game" else default
        )

        await real_servicer.StartMenu(menu_pb2.StartMenuRequest(), MagicMock())

        assert real_servicer.current_selection == Games.Tournament
        assert real_servicer.state_manager.current_game_mode == Games.Tournament

    @pytest.mark.asyncio
    async def test_start_menu_ignores_unplayable_persisted_selection(self, real_servicer):
        """A persisted mode outside GAME_MODES falls back to the default mode."""
        real_servicer.current_selection = Games.Tournament
        real_servicer.user_prefs_client.get_string_value.side_effect = lambda key, default, *_a, **_k: (
            "Random" if key == "current_game" else default
        )

        await real_servicer.StartMenu(menu_pb2.StartMenuRequest(), MagicMock())

        assert real_servicer.current_selection == Games.JoustFFA

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A11")
    async def test_start_menu_already_running(self, real_servicer):
        """StartMenu on a running menu fails without side effects."""
        real_servicer.state = menu_pb2.MenuState.RUNNING

        response = await real_servicer.StartMenu(menu_pb2.StartMenuRequest(), MagicMock())

        assert response.success is False
        assert "already running" in response.error
        real_servicer.audio.start_lobby_music.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A11")
    async def test_stop_menu_clears_lobby(self, real_servicer):
        """StopMenu sets STOPPED, stops the idle monitor, clears the lobby, publishes menu_stopped."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        await _connect(real_servicer, "s1", "s2")

        response = await real_servicer.StopMenu(menu_pb2.StopMenuRequest(), MagicMock())

        assert response.success is True
        assert real_servicer.state == menu_pb2.MenuState.STOPPED
        assert real_servicer.connected_controllers == set()
        assert real_servicer.state_manager.controller_states == {}
        real_servicer.idle_monitor.stop.assert_called_once()
        assert ("menu_stopped", {}) in _published(real_servicer)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A11")
    async def test_stop_menu_releases_admin_lock_and_clears_controllers(self, real_servicer):
        """StopMenu clears every controller state and calls abandon_session to release the admin lock."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        await _connect(real_servicer, "s1", "s2")
        await _enter_admin(real_servicer, "s1")
        assert real_servicer.admin_handler.active is True
        handler = real_servicer.admin_handler

        with patch.object(handler, "abandon_session", autospec=True, side_effect=handler.abandon_session) as abandon:
            await real_servicer.StopMenu(menu_pb2.StopMenuRequest(), MagicMock())

        abandon.assert_called_once()
        assert handler.active is False
        assert real_servicer.state_manager.controller_states == {}

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A11")
    async def test_stop_menu_already_stopped(self, real_servicer):
        """StopMenu on a stopped menu fails."""
        response = await real_servicer.StopMenu(menu_pb2.StopMenuRequest(), MagicMock())

        assert response.success is False
        assert "already stopped" in response.error


class TestMenuServicerProcessInput:
    """ProcessInput button_press / web_command / reset_menu on the real servicer."""

    @staticmethod
    def _request(input_type: str, **data: str):
        return menu_pb2.ProcessInputRequest(input_type=input_type, data=data)

    @pytest.mark.asyncio
    async def test_button_select_cycles_and_persists_mode(self, real_servicer):
        """button_press select advances to the next playable mode, publishes and persists it."""
        from services.menu.utils.settings import get_next_game_mode

        expected = get_next_game_mode(Games.JoustFFA)

        response = await real_servicer.ProcessInput(self._request("button_press", button="select"), MagicMock())

        assert response.success is True
        assert real_servicer.current_selection == expected
        assert real_servicer.state_manager.current_game_mode == expected
        assert ("selection_changed", {"game_name": expected.name, "source": "button_input"}) in _published(
            real_servicer
        )
        real_servicer.user_prefs_writer.update_default_variant.assert_called_once_with("current_game", expected.name)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A6")
    async def test_button_trigger_starts_with_ready_roster(self, real_servicer):
        """UC-010 A6: a ProcessInput trigger starts with the READY controllers, not waiting for all."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        await _connect(real_servicer, "s1", "s2", "s3")
        await _ready_up(real_servicer, "s1", "s2")
        assert real_servicer._game_lifecycle_task is None  # s3 not ready: no auto start

        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as stream:
            await real_servicer.ProcessInput(self._request("button_press", button="trigger"), MagicMock())
            await _await_lifecycle(real_servicer)

        assert _started_roster(stream) == ["s1", "s2"]
        assert real_servicer.state == menu_pb2.MenuState.GAME_STARTING

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A6")
    async def test_web_start_game_starts_with_ready_roster(self, real_servicer):
        """UC-010 A6: web_command start_game starts the selected mode with the READY controllers."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        real_servicer.current_selection = Games.JoustTeams
        await _connect(real_servicer, "s1", "s2", "s3", "s4")
        await _ready_up(real_servicer, "s2", "s3", "s4")

        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as stream:
            await real_servicer.ProcessInput(self._request("web_command", command="start_game"), MagicMock())
            await _await_lifecycle(real_servicer)

        assert _started_roster(stream) == ["s2", "s3", "s4"]
        request = stream.await_args.args[2]
        assert request.start_config.game_name == Games.JoustTeams.name

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A3", steps=[1, 2], br=["BR-001"])
    @pytest.mark.uc("UC-010", flow="A6")
    async def test_web_start_game_below_mode_minimum_is_cancelled(self, real_servicer):
        """web_command start_game for Teams with two READY players never reaches the coordinator."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        real_servicer.current_selection = Games.JoustTeams
        await _connect(real_servicer, "s1", "s2", "s3")
        await _ready_up(real_servicer, "s1", "s2")

        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as stream:
            await real_servicer.ProcessInput(self._request("web_command", command="start_game"), MagicMock())
            await _await_lifecycle(real_servicer)

        stream.assert_not_awaited()
        assert _published(real_servicer)[-1] == (
            "game_start_cancelled",
            {"game_name": "JoustTeams", "player_count": 2, "minimum_players": 3},
        )
        assert real_servicer.state == menu_pb2.MenuState.RUNNING
        assert set(real_servicer.state_manager.controller_states.values()) == {ControllerState.CONNECTED}

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A3", steps=[1, 2], br=["BR-001"])
    async def test_button_trigger_below_mode_minimum_is_cancelled(self, real_servicer):
        """A ProcessInput trigger for Teams with two READY players is cancelled the same way."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        _select(real_servicer, Games.JoustTeams)
        await _connect(real_servicer, "s1", "s2", "s3")
        await _ready_up(real_servicer, "s1", "s2")

        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as stream:
            await real_servicer.ProcessInput(self._request("button_press", button="trigger"), MagicMock())
            await _await_lifecycle(real_servicer)

        stream.assert_not_awaited()
        assert _published(real_servicer)[-1] == (
            "game_start_cancelled",
            {"game_name": "JoustTeams", "player_count": 2, "minimum_players": 3},
        )
        assert set(real_servicer.state_manager.controller_states.values()) == {ControllerState.CONNECTED}

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A6")
    async def test_web_start_game_honours_requested_mode(self, real_servicer):
        """UC-010 A6 (#1278): start_game with a mode selects that mode, then starts it."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        real_servicer.current_selection = Games.JoustTeams
        await _connect(real_servicer, "s1", "s2", "s3")
        await _ready_up(real_servicer, "s1", "s2")

        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as stream:
            response = await real_servicer.ProcessInput(
                self._request("web_command", command="start_game", mode="JoustFFA"), MagicMock()
            )
            await _await_lifecycle(real_servicer)

        assert response.success is True
        assert stream.await_args.args[2].start_config.game_name == Games.JoustFFA.name
        assert real_servicer.current_selection == Games.JoustFFA
        assert real_servicer.state_manager.current_game_mode == Games.JoustFFA
        assert ("selection_changed", {"game_name": "JoustFFA", "source": "web"}) in _published(real_servicer)
        real_servicer.user_prefs_writer.update_default_variant.assert_called_once_with("current_game", "JoustFFA")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A6")
    async def test_web_start_game_accepts_mode_alias(self, real_servicer):
        """UC-010 A6 (#1278): an alias ("ffa") resolves to its mode and starts it."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        real_servicer.current_selection = Games.JoustTeams
        await _connect(real_servicer, "s1", "s2", "s3")
        await _ready_up(real_servicer, "s1", "s2")

        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as stream:
            response = await real_servicer.ProcessInput(
                self._request("web_command", command="start_game", mode="ffa"), MagicMock()
            )
            await _await_lifecycle(real_servicer)

        assert response.success is True
        assert stream.await_args.args[2].start_config.game_name == Games.JoustFFA.name
        assert real_servicer.current_selection == Games.JoustFFA

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A6")
    @pytest.mark.uc("UC-011", br=["BR-002"])
    async def test_web_start_game_mode_survives_select_during_start_delay(self, real_servicer):
        """UC-010 A6 (#1278): a lobby SELECT during the start delay does not swap the requested mode."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        real_servicer.current_selection = Games.JoustTeams
        await _connect(real_servicer, "s1", "s2", "s3")
        await _ready_up(real_servicer, "s1", "s2")

        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as stream:
            await real_servicer.ProcessInput(
                self._request("web_command", command="start_game", mode="JoustFFA"), MagicMock()
            )
            # The lifecycle is still in its start delay: a SELECT changes the lobby mode now.
            assert not real_servicer._game_lifecycle_task.done()
            await real_servicer.select_game_mode("Tournament", source="controller")
            await _await_lifecycle(real_servicer)

        assert real_servicer.current_selection == Games.Tournament
        assert stream.await_args.args[2].start_config.game_name == Games.JoustFFA.name

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A6")
    @pytest.mark.parametrize("mode", ["Commander", "Ninja", "Random", "NotAGame"])
    @pytest.mark.uc("UC-011", br=["BR-002"])
    async def test_web_start_game_rejects_invalid_mode(self, real_servicer, mode):
        """UC-010 A6 (#1278): an unknown or unimplemented mode is rejected and no game starts."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        real_servicer.current_selection = Games.JoustFFA
        await _connect(real_servicer, "s1", "s2", "s3")
        await _ready_up(real_servicer, "s1", "s2")
        real_servicer.event_publisher.publish.reset_mock()

        response = await real_servicer.ProcessInput(
            self._request("web_command", command="start_game", mode=mode), MagicMock()
        )

        assert response.success is False
        assert mode in response.error
        assert real_servicer._game_lifecycle_task is None
        assert real_servicer.current_selection == Games.JoustFFA
        assert _published(real_servicer) == [
            ("game_start_rejected", {"game_name": mode, "source": "web", "error": response.error})
        ]
        real_servicer.user_prefs_writer.update_default_variant.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", br=["BR-001", "BR-002"])
    async def test_list_game_modes_offers_playable_modes_with_ffa_default(self, real_servicer):
        """UC-011 BR-001/BR-002 (#1278): the dashboard selector's modes are the playable ones."""
        response = await real_servicer.ListGameModes(menu_pb2.ListGameModesRequest(), MagicMock())

        assert response.default_mode == Games.JoustFFA.name
        assert response.game_modes[0] == Games.JoustFFA.name
        # The ten modes registered in the coordinator's GameFactory (_GAME_CLASSES).
        assert set(response.game_modes) == {
            "JoustFFA",
            "JoustTeams",
            "JoustRandomTeams",
            "Traitor",
            "Werewolf",
            "Zombies",
            "Swapper",
            "FightClub",
            "Tournament",
            "NonStop",
        }

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A10", br=["BR-008"])
    async def test_reset_menu_cancels_game_start(self, real_servicer):
        """reset_menu in GAME_STARTING returns to RUNNING and publishes game_start_cancelled."""
        real_servicer.state = menu_pb2.MenuState.GAME_STARTING

        await real_servicer.ProcessInput(self._request("reset_menu"), MagicMock())

        assert real_servicer.state == menu_pb2.MenuState.RUNNING
        assert _published(real_servicer) == [("game_start_cancelled", {})]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A10")
    async def test_reset_menu_while_running_is_noop(self, real_servicer):
        """reset_menu outside GAME_STARTING changes nothing and publishes nothing."""
        real_servicer.state = menu_pb2.MenuState.RUNNING

        await real_servicer.ProcessInput(self._request("reset_menu"), MagicMock())

        assert real_servicer.state == menu_pb2.MenuState.RUNNING
        real_servicer.event_publisher.publish.assert_not_awaited()


class TestMenuServicerReadyUpStart:
    """UC-010: ready up in the lobby and start the game (real state manager + handlers)."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="main", steps=[1, 2, 3, 4, 5], br=["BR-001"])
    async def test_all_ready_starts_selected_mode_with_ready_players(self, real_servicer):
        """UC-010 steps 1-5: everyone readies up, the menu starts the selected mode with them."""
        from lib.types import Sound

        real_servicer.state = menu_pb2.MenuState.RUNNING
        real_servicer.current_selection = Games.JoustFFA
        await _connect(real_servicer, "s1")

        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as stream:
            await _ready_up(real_servicer, "s1")
            # BR-001: a lone ready player is "everyone ready" but below the 2-player minimum.
            assert real_servicer.ready_controllers == real_servicer.connected_controllers == {"s1"}
            assert real_servicer._game_lifecycle_task is None
            await _connect(real_servicer, "s2")
            await _ready_up(real_servicer, "s2")
            await _await_lifecycle(real_servicer)

        # Step 2: beep + bright LED per player.
        real_servicer.audio.play_sound.assert_any_await(Sound.SFX_BEEP_LOUD, volume=0.5)
        real_servicer.led.set_ready_color.assert_any_await("s1", Games.JoustFFA.name)
        real_servicer.led.set_ready_color.assert_any_await("s2", Games.JoustFFA.name)
        # Step 5: lobby music + idle monitor stopped, the coordinator asked for the mode.
        real_servicer.audio.stop_lobby_music.assert_awaited_once()
        real_servicer.idle_monitor.stop.assert_called()
        assert _started_roster(stream) == ["s1", "s2"]
        assert stream.await_args.args[2].start_config.game_name == Games.JoustFFA.name
        assert real_servicer.state == menu_pb2.MenuState.GAME_STARTING

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A1")
    async def test_move_unreadies_and_blocks_start(self, real_servicer):
        """UC-010 A1: a READY player pressing Move returns to CONNECTED and no game starts."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        await _connect(real_servicer, "s1", "s2")
        await _ready_up(real_servicer, "s1")

        await real_servicer.on_button("s1", "move", True)

        assert real_servicer.state_manager.controller_states["s1"] == ControllerState.CONNECTED
        real_servicer.led.set_connected_color.assert_awaited_with("s1", Games.JoustFFA.name)
        assert real_servicer._game_lifecycle_task is None

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A4")
    async def test_second_trigger_starts_once(self, real_servicer):
        """UC-010 A4: with everyone ready, a trigger press starts the game; the latch stops a second start.

        All-ready is reached without an on_enter (the last un-ready controller
        disconnects), so the trigger path is what starts the game.
        """
        real_servicer.state = menu_pb2.MenuState.RUNNING
        await _connect(real_servicer, "s1", "s2", "s3")
        await _ready_up(real_servicer, "s1", "s2")
        await real_servicer.on_disconnect("s3")
        assert real_servicer.state_manager.all_ready()
        assert real_servicer._game_lifecycle_task is None

        with patch.object(type(real_servicer), "_run_game_lifecycle", autospec=True) as lifecycle:
            await real_servicer.on_button("s1", "trigger", True)
            await real_servicer.on_button("s2", "trigger", True)
            await _await_lifecycle(real_servicer)

        lifecycle.assert_called_once_with(real_servicer, "s1", "controller", None, game_mode=None)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="main", steps=[4], br=["BR-002"])
    @pytest.mark.parametrize("blocking_state", [ControllerState.IDLE, ControllerState.ADMIN])
    async def test_idle_or_admin_controller_blocks_auto_start(self, real_servicer, blocking_state):
        """UC-010 BR-002: an IDLE or ADMIN controller counts as connected but not ready."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        await _connect(real_servicer, "s1", "s2")
        real_servicer.state_manager.controller_states["blocker"] = blocking_state

        await _ready_up(real_servicer, "s1", "s2")

        assert real_servicer.ready_controllers == {"s1", "s2"}
        assert not real_servicer.state_manager.all_ready()
        assert real_servicer._game_lifecycle_task is None

        # Allowed case: without the blocker the same lobby is all ready.
        del real_servicer.state_manager.controller_states["blocker"]
        assert real_servicer.state_manager.all_ready()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A3", steps=[1, 2])
    async def test_short_roster_start_returns_to_lobby_unreadied(self, real_servicer):
        """UC-010 A3 + failure postcondition: a start with one READY player is cancelled and the
        lobby is restored: no game, the READY player is CONNECTED again with the lobby colour,
        lobby music is not touched and the selection is unchanged."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        real_servicer.current_selection = Games.JoustTeams
        real_servicer.state_manager.set_game_mode(Games.JoustTeams)
        await _connect(real_servicer, "s1", "s2", "s3")
        await _ready_up(real_servicer, "s1")
        led = real_servicer.state_manager.led
        led.reset_mock()
        real_servicer.event_publisher.publish.reset_mock()

        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as stream:
            await real_servicer.ProcessInput(
                menu_pb2.ProcessInputRequest(input_type="button_press", data={"button": "trigger"}), MagicMock()
            )
            await _await_lifecycle(real_servicer)

        stream.assert_not_awaited()
        assert _published(real_servicer) == [
            ("game_start_cancelled", {"game_name": "JoustTeams", "player_count": 1, "minimum_players": 3})
        ]
        assert real_servicer.state == menu_pb2.MenuState.RUNNING
        assert real_servicer.state_manager.controller_states == {
            "s1": ControllerState.CONNECTED,
            "s2": ControllerState.CONNECTED,
            "s3": ControllerState.CONNECTED,
        }
        led.set_connected_color.assert_any_await("s1", Games.JoustTeams.name)
        real_servicer.audio.stop_lobby_music.assert_not_awaited()
        real_servicer.idle_monitor.stop.assert_not_called()
        assert real_servicer.current_selection == Games.JoustTeams

        # The un-ready cleared the start latch: the players can ready up and the next start goes through.
        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as second_stream:
            await _ready_up(real_servicer, "s1", "s2", "s3")
            await _await_lifecycle(real_servicer)

        assert _started_roster(second_stream) == ["s1", "s2", "s3"]
        assert second_stream.await_args.args[2].start_config.game_name == Games.JoustTeams.name
        assert real_servicer.state == menu_pb2.MenuState.GAME_STARTING

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A3", steps=[1, 2])
    async def test_coordinator_start_error_returns_to_lobby_unreadied(self, real_servicer):
        """UC-010 A3: the coordinator answers the start with game_start_error: every player is CONNECTED
        again with the lobby colour, lobby music and idle monitor run, and the selection is unchanged."""
        from types import SimpleNamespace

        real_servicer.state = menu_pb2.MenuState.RUNNING
        real_servicer.current_selection = Games.JoustTeams
        real_servicer.state_manager.set_game_mode(Games.JoustTeams)
        await _connect(real_servicer, "s1", "s2", "s3")
        led = real_servicer.state_manager.led
        real_servicer.audio.start_lobby_music.reset_mock()
        real_servicer.idle_monitor.start.reset_mock()

        class RejectingStub:
            def __init__(self, _channel):
                pass

            def StreamGameEvents(self, _request, metadata=None):  # noqa: N802 - gRPC stub name
                async def events():
                    yield SimpleNamespace(event_type="game_start_error", data={"error": "game already running"})

                stream = events()
                stream.cancel = lambda: None
                return stream

        with patch("proto.game_coordinator_pb2_grpc.GameCoordinatorServiceStub", RejectingStub):
            await _ready_up(real_servicer, "s1", "s2", "s3")
            led.reset_mock()
            await _await_lifecycle(real_servicer)

        assert real_servicer.state == menu_pb2.MenuState.RUNNING
        assert real_servicer.state_manager.controller_states == {
            "s1": ControllerState.CONNECTED,
            "s2": ControllerState.CONNECTED,
            "s3": ControllerState.CONNECTED,
        }
        assert real_servicer.state_manager.ready_controllers == set()
        assert sorted(c.args for c in led.set_connected_color.await_args_list) == [
            ("s1", Games.JoustTeams.name),
            ("s2", Games.JoustTeams.name),
            ("s3", Games.JoustTeams.name),
        ]
        real_servicer.audio.start_lobby_music.assert_awaited_once()
        real_servicer.idle_monitor.start.assert_called_once()
        assert real_servicer.current_selection == Games.JoustTeams


class TestMenuServicerForceStartShortRoster:
    """UC-014 A3: a forced roster of fewer than two players never starts a game."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="A3", steps=[1, 2])
    async def test_force_start_with_only_host_publishes_cancelled(self, real_servicer):
        """force_all_start off and nobody else ready: roster = host only -> game_start_cancelled, lobby back."""
        from proto import controller_manager_pb2, controller_manager_pb2_grpc

        real_servicer.state = menu_pb2.MenuState.RUNNING
        await _connect(real_servicer, "host", "s2")
        for button in ("cross", "circle", "square", "triangle"):
            await real_servicer.on_button("host", button, True)
        assert real_servicer.admin_handler.is_admin_controller("host")

        live_stub = create_autospec(controller_manager_pb2_grpc.ControllerManagerServiceStub, instance=True)
        live_stub.GetConnectedControllers = AsyncMock(
            return_value=controller_manager_pb2.GetConnectedControllersResponse(connected_serials=["host", "s2"])
        )
        with (
            patch("lib.feature_flags.get_flag_client", side_effect=default_returning_flag_client),
            patch.object(controller_manager_pb2_grpc, "ControllerManagerServiceStub", return_value=live_stub),
            patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as stream,
        ):
            await real_servicer.admin_handler.handle_force_start("host")
            await _await_lifecycle(real_servicer)

        published = [event for event, _data in _published(real_servicer)]
        assert "game_requested" in published
        assert published.count("game_start_cancelled") == 1
        assert published.index("game_requested") < published.index("game_start_cancelled")
        assert real_servicer.state == menu_pb2.MenuState.RUNNING
        stream.assert_not_awaited()
        # Back in the lobby: the host is CONNECTED again with the lobby colour.
        assert real_servicer.state_manager.controller_states["host"] == ControllerState.CONNECTED
        real_servicer.led.set_connected_color.assert_any_await("host", Games.JoustFFA.name)


# Per-mode minimum players (UC-010 BR-001, #1862), pinned so a changed minimum fails a test.
_MODE_MINIMUMS = [
    (Games.JoustFFA, 2),
    (Games.JoustTeams, 3),
    (Games.JoustRandomTeams, 3),
    (Games.Traitor, 4),
    (Games.Werewolf, 3),
    (Games.Zombies, 4),
    (Games.Swapper, 3),
    (Games.FightClub, 2),
    (Games.Tournament, 3),
    (Games.NonStop, 2),
]


async def _enter_admin(servicer, serial: str) -> None:
    """Hold Cross, Circle, Square and Triangle (UC-012), then release them."""
    for button in ("cross", "circle", "square", "triangle"):
        await servicer.on_button(serial, button, True)
    for button in ("cross", "circle", "square", "triangle"):
        await servicer.on_button(serial, button, False)
    assert servicer.admin_handler.is_admin_controller(serial)


def _select(servicer, mode: Games) -> None:
    servicer.current_selection = mode
    servicer.state_manager.set_game_mode(mode)


def _not_enough(mode: Games, count: int, minimum: int) -> tuple[str, dict]:
    return ("game_start_cancelled", {"game_name": mode.name, "player_count": count, "minimum_players": minimum})


class TestModeMinimumPlayers:
    """UC-010 BR-001 / UC-014 BR-002: no start below the selected mode's minimum, on every start path."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A3", steps=[1, 2], br=["BR-001"])
    @pytest.mark.uc("UC-021", cond=["Pre-1"])
    @pytest.mark.uc("UC-022", cond=["Pre-1"])
    @pytest.mark.uc("UC-023", cond=["Pre-1"])
    @pytest.mark.uc("UC-024", cond=["Pre-1"])
    @pytest.mark.uc("UC-025", cond=["Pre-1"])
    @pytest.mark.parametrize(("mode", "minimum"), [m for m in _MODE_MINIMUMS if m[1] > 2], ids=lambda v: str(v))
    async def test_everyone_ready_below_minimum_is_cancelled_then_starts_at_minimum(self, real_servicer, mode, minimum):
        """Everyone ready but one short of the mode's minimum: no coordinator start, game_start_cancelled
        names the shortfall, "not enough players" is announced, everyone is un-readied (dim lobby LED).
        With one more player the same lobby starts the mode."""
        from lib.types import Sound

        real_servicer.state = menu_pb2.MenuState.RUNNING
        _select(real_servicer, mode)
        short = [f"s{i}" for i in range(1, minimum)]
        await _connect(real_servicer, *short)
        led = real_servicer.state_manager.led

        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as stream:
            await _ready_up(real_servicer, *short)
            await _await_lifecycle(real_servicer)

        stream.assert_not_awaited()
        assert _published(real_servicer)[-1] == _not_enough(mode, minimum - 1, minimum)
        real_servicer.audio.play_voice.assert_awaited_with(Sound.MENU_VOX_NOT_ENOUGH_PLAYERS)
        assert real_servicer.state == menu_pb2.MenuState.RUNNING
        assert set(real_servicer.state_manager.controller_states.values()) == {ControllerState.CONNECTED}
        led.set_connected_color.assert_any_await("s1", mode.name)
        assert real_servicer.current_selection == mode

        full = [*short, f"s{minimum}"]
        await _connect(real_servicer, full[-1])
        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as second_stream:
            await _ready_up(real_servicer, *full)
            await _await_lifecycle(real_servicer)

        assert _started_roster(second_stream) == sorted(full)
        assert second_stream.await_args.args[2].start_config.game_name == mode.name
        assert real_servicer.state == menu_pb2.MenuState.GAME_STARTING

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A3", steps=[1, 2], br=["BR-001"])
    @pytest.mark.parametrize(("mode", "minimum"), _MODE_MINIMUMS, ids=lambda v: str(v))
    async def test_short_roster_is_cancelled_for_every_mode(self, real_servicer, mode, minimum):
        """Every implemented mode: a roster one below its minimum never reaches the coordinator."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        _select(real_servicer, mode)
        roster = [f"s{i}" for i in range(1, minimum)]

        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as stream:
            await real_servicer.start_game("s1", source="controller", controllers=roster)
            await _await_lifecycle(real_servicer)

        stream.assert_not_awaited()
        assert _published(real_servicer) == [_not_enough(mode, minimum - 1, minimum)]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="A3", steps=[1, 2], br=["BR-002"])
    @pytest.mark.uc("UC-022", cond=["Pre-1"])
    async def test_force_start_skips_ready_check_but_not_minimum(self, real_servicer):
        """UC-014 BR-002: a forced Traitor roster of three is cancelled; with four it starts though
        the host is not READY."""
        from lib.types import Sound
        from proto import controller_manager_pb2_grpc

        real_servicer.state = menu_pb2.MenuState.RUNNING
        _select(real_servicer, Games.Traitor)
        await _connect(real_servicer, "host", "s2", "s3", "s4")
        await _enter_admin(real_servicer, "host")
        await _ready_up(real_servicer, "s2", "s3")

        live_stub = create_autospec(controller_manager_pb2_grpc.ControllerManagerServiceStub, instance=True)
        live_stub.GetConnectedControllers = AsyncMock(
            return_value=controller_manager_pb2.GetConnectedControllersResponse(
                connected_serials=["host", "s2", "s3", "s4"]
            )
        )
        with (
            patch("lib.feature_flags.get_flag_client", side_effect=default_returning_flag_client),
            patch.object(controller_manager_pb2_grpc, "ControllerManagerServiceStub", return_value=live_stub),
            patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as stream,
        ):
            await real_servicer.admin_handler.handle_force_start("host")
            await _await_lifecycle(real_servicer)

        stream.assert_not_awaited()
        assert _published(real_servicer)[-1] == _not_enough(Games.Traitor, 3, 4)
        real_servicer.audio.play_voice.assert_awaited_with(Sound.MENU_VOX_NOT_ENOUGH_PLAYERS)
        assert real_servicer.state == menu_pb2.MenuState.RUNNING

        # Four in the roster: the host (admin) is not READY, the force-start still starts the game.
        await _enter_admin(real_servicer, "host")
        await _ready_up(real_servicer, "s2", "s3", "s4")
        assert real_servicer._game_lifecycle_task.done()
        with (
            patch("lib.feature_flags.get_flag_client", side_effect=default_returning_flag_client),
            patch.object(controller_manager_pb2_grpc, "ControllerManagerServiceStub", return_value=live_stub),
            patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as second_stream,
        ):
            await real_servicer.admin_handler.handle_force_start("host")
            await _await_lifecycle(real_servicer)

        assert _started_roster(second_stream) == ["host", "s2", "s3", "s4"]
        assert second_stream.await_args.args[2].start_config.game_name == Games.Traitor.name


class TestStartWhileGameActive:
    """UC-010 A8 / BR-008 (#2054): a start while a game is starting or running is refused, never cancels it."""

    @staticmethod
    async def _live_game(servicer, release: asyncio.Event):
        """Start a game whose coordinator stream stays open until ``release`` is set; return its task."""
        servicer.state = menu_pb2.MenuState.RUNNING
        await _connect(servicer, "s1", "s2")

        async def _open_stream(*_args, **_kwargs):
            await release.wait()

        with patch.object(type(servicer), "_run_game_event_stream", autospec=True, side_effect=_open_stream) as stream:
            await _ready_up(servicer, "s1", "s2")
            for _ in range(20):
                if stream.await_count:
                    break
                await asyncio.sleep(0.05)
            assert stream.await_count == 1
            task = servicer._game_lifecycle_task
            assert task is not None
            assert not task.done()
            return task, stream

    @staticmethod
    def _rejection(source: str) -> tuple[str, dict]:
        return (
            "game_start_rejected",
            {"game_name": Games.JoustFFA.name, "source": source, "reason": "game_active"},
        )

    async def _assert_refused(self, servicer, task, stream, source: str, release: asyncio.Event) -> None:
        assert servicer._game_lifecycle_task is task
        assert not task.cancelled()
        assert not task.done()
        assert stream.await_count == 1
        assert self._rejection(source) in _published(servicer)
        # Silent while a game is starting or running (LED is the primary cue; A8).
        assert not self._beeped(servicer)
        release.set()
        await task

    @staticmethod
    def _beeped(servicer) -> bool:
        from lib.types import Sound

        # The refusal beep is the only full-volume one (the ready-up beep passes volume=0.5).
        return any(c.args == (Sound.SFX_BEEP_LOUD,) and not c.kwargs for c in servicer.audio.play_sound.await_args_list)

    @staticmethod
    def _game_state(servicer, state=None, *, success: bool = True, error: Exception | None = None) -> AsyncMock:
        """Answer the coordinator's GetGameState through the real stub on the autospec'd channel."""
        from proto import game_coordinator_pb2

        if error is not None:
            call = AsyncMock(side_effect=error)
        else:
            response = game_coordinator_pb2.GetGameStateResponse(
                success=success, game_info=game_coordinator_pb2.GameInfo(state=state)
            )
            call = AsyncMock(return_value=response)
        servicer.game_coordinator_channel.unary_unary.return_value = call
        return call

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A8", br=["BR-008"])
    async def test_ready_auto_start_mid_game_is_refused(self, real_servicer):
        """Ready auto start path (latch cleared by a reset): the live lifecycle survives."""
        release = asyncio.Event()
        task, stream = await self._live_game(real_servicer, release)
        real_servicer.ready_handler.reset_game_start_flag()
        await _connect(real_servicer, "s3")
        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as second:
            await _ready_up(real_servicer, "s3")
            second.assert_not_awaited()
        await self._assert_refused(real_servicer, task, stream, "controller", release)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A8", br=["BR-008"])
    async def test_trigger_button_input_mid_game_is_refused(self, real_servicer):
        """ProcessInput trigger button mid-game."""
        release = asyncio.Event()
        task, stream = await self._live_game(real_servicer, release)
        request = menu_pb2.ProcessInputRequest(input_type="button_press", data={"button": "trigger"})
        await real_servicer.ProcessInput(request, MagicMock())
        await self._assert_refused(real_servicer, task, stream, "button_input", release)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A8", br=["BR-008"])
    async def test_web_start_game_mid_game_is_refused(self, real_servicer):
        """ProcessInput web start_game mid-game."""
        release = asyncio.Event()
        task, stream = await self._live_game(real_servicer, release)
        request = menu_pb2.ProcessInputRequest(input_type="web_command", data={"command": "start_game"})
        await real_servicer.ProcessInput(request, MagicMock())
        await self._assert_refused(real_servicer, task, stream, "web", release)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A8", br=["BR-008"])
    @pytest.mark.uc("UC-014", flow="A4")
    async def test_admin_force_start_mid_game_is_refused(self, real_servicer):
        """Admin force-start callback mid-game."""
        release = asyncio.Event()
        task, stream = await self._live_game(real_servicer, release)
        await real_servicer.start_game("s1", source="admin_force_start", controllers=["s1", "s2"])
        await self._assert_refused(real_servicer, task, stream, "admin_force_start", release)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A8", br=["BR-008"])
    async def test_finished_lifecycle_does_not_block_next_start(self, real_servicer):
        """BR-008: a lifecycle task that already finished is replaced, not refused."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        await _connect(real_servicer, "s1", "s2")
        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as first:
            await _ready_up(real_servicer, "s1", "s2")
            await _await_lifecycle(real_servicer)
        first.assert_awaited_once()
        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as second:
            await real_servicer.start_game("s1", source="web", controllers=["s1", "s2"])
            await _await_lifecycle(real_servicer)
        second.assert_awaited_once()
        assert not any(e[0] == "game_start_rejected" for e in _published(real_servicer))

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A8", br=["BR-008"])
    async def test_start_in_pre_game_starting_window_is_refused(self, real_servicer):
        """A start in the window before GAME_STARTING (lifecycle scheduled, lobby still up) is refused silently."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        await _connect(real_servicer, "s1", "s2")
        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as stream:
            await real_servicer.start_game("s1", source="web", controllers=["s1", "s2"])
            task = real_servicer._game_lifecycle_task
            assert task is not None
            assert not task.done()
            assert real_servicer.state == menu_pb2.MenuState.RUNNING
            await real_servicer.start_game("s2", source="admin_force_start", controllers=["s1", "s2"])
            assert real_servicer._game_lifecycle_task is task
            assert self._rejection("admin_force_start") in _published(real_servicer)
            assert not self._beeped(real_servicer)
            await task
        stream.assert_awaited_once()
        assert not task.cancelled()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A8", br=["BR-008"])
    @pytest.mark.uc("UC-010", flow="A4")
    async def test_refused_ready_start_in_lobby_restart_window_then_trigger_starts(self, real_servicer):
        """A ready auto start refused in the real _restart_lobby window beeps and clears the latch.

        The lifecycle task is still restoring the lobby (lobby music pending) when every player is
        ready again: the start is refused with a beep, the latch is cleared, and once the task has
        finished a trigger press starts the next game.
        """
        real_servicer.state = menu_pb2.MenuState.RUNNING
        await _connect(real_servicer, "s1", "s2")
        music_gate = asyncio.Event()
        in_window = asyncio.Event()

        async def _music_blocks():
            in_window.set()
            await music_gate.wait()

        async def _game_then_real_restart(servicer, *_args):
            await servicer._restart_lobby()

        real_servicer.audio.start_lobby_music.side_effect = _music_blocks
        with patch.object(
            type(real_servicer), "_run_game_event_stream", autospec=True, side_effect=_game_then_real_restart
        ):
            await _ready_up(real_servicer, "s1", "s2")
            task = real_servicer._game_lifecycle_task
            await asyncio.wait_for(in_window.wait(), timeout=2)
            assert not task.done()
            # state_manager.reset() ran: everyone is back in CONNECTED; all ready up again.
            await _ready_up(real_servicer, "s1", "s2")
            assert self._rejection("controller") in _published(real_servicer)
            assert self._beeped(real_servicer)
            assert real_servicer.ready_handler._game_start_in_progress is False
            real_servicer.audio.start_lobby_music.side_effect = None
            music_gate.set()
            await task

        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as second:
            await real_servicer.on_button("s1", "trigger", True)
            await _await_lifecycle(real_servicer)
        second.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A8", br=["BR-008"])
    @pytest.mark.parametrize("state_name", ["STARTING", "RUNNING"])
    async def test_reset_menu_keeps_lifecycle_of_active_game(self, real_servicer, state_name):
        """reset_menu while the coordinator reports a starting or running game keeps the lifecycle, refuses starts."""
        from proto import game_coordinator_pb2

        release = asyncio.Event()
        task, stream = await self._live_game(real_servicer, release)
        lookup = self._game_state(real_servicer, game_coordinator_pb2.GameState.Value(state_name))
        await real_servicer.ProcessInput(menu_pb2.ProcessInputRequest(input_type="reset_menu"), MagicMock())
        lookup.assert_awaited_once()
        await real_servicer.start_game("s1", source="web", controllers=["s1", "s2"])
        await self._assert_refused(real_servicer, task, stream, "web", release)
        assert ("game_start_cancelled", {}) not in _published(real_servicer)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A8", br=["BR-008"])
    async def test_reset_menu_lookup_failure_keeps_lifecycle(self, real_servicer):
        """BR-008: a failed GetGameState fails closed: the lifecycle is not cancelled."""
        release = asyncio.Event()
        task, stream = await self._live_game(real_servicer, release)
        self._game_state(real_servicer, error=RuntimeError("coordinator unreachable"))
        await real_servicer.ProcessInput(menu_pb2.ProcessInputRequest(input_type="reset_menu"), MagicMock())
        assert real_servicer._game_lifecycle_task is task
        assert not task.done()
        release.set()
        await task

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A8", br=["BR-008"])
    async def test_reset_menu_recovers_a_lifecycle_with_no_active_game(self, real_servicer):
        """A lifecycle task that never finishes (the game already ended) is cancelled by reset_menu.

        The lobby is restored and the next start runs a new game.
        """
        from proto import game_coordinator_pb2

        never = asyncio.Event()
        task, _stream = await self._live_game(real_servicer, never)
        real_servicer.controller_events.start.reset_mock()
        lookup = self._game_state(real_servicer, game_coordinator_pb2.GameState.ENDED)
        await real_servicer.ProcessInput(menu_pb2.ProcessInputRequest(input_type="reset_menu"), MagicMock())
        lookup.assert_awaited_once()
        assert task.cancelled()
        assert real_servicer._game_lifecycle_task is None
        assert real_servicer.state == menu_pb2.MenuState.RUNNING
        real_servicer.controller_events.start.assert_awaited_once()
        assert ("game_start_cancelled", {}) in _published(real_servicer)

        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as second:
            await real_servicer.start_game("s1", source="web", controllers=["s1", "s2"])
            await _await_lifecycle(real_servicer)
        second.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A8", br=["BR-008"])
    async def test_reset_menu_unsuccessful_lookup_keeps_lifecycle(self, real_servicer):
        """BR-008: an unsuccessful GetGameState answer fails closed: the lifecycle is not cancelled."""
        release = asyncio.Event()
        task, _stream = await self._live_game(real_servicer, release)
        self._game_state(real_servicer, success=False)
        await real_servicer.ProcessInput(menu_pb2.ProcessInputRequest(input_type="reset_menu"), MagicMock())
        assert real_servicer._game_lifecycle_task is task
        assert not task.done()
        assert ("game_start_cancelled", {}) not in _published(real_servicer)
        release.set()
        await task

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A8", br=["BR-008"])
    async def test_reset_menu_ignores_a_lifecycle_replaced_during_lookup(self, real_servicer):
        """The old lifecycle ends and a new start begins while GetGameState is in flight: the new game is kept."""
        from proto import game_coordinator_pb2

        old_release = asyncio.Event()
        old_task, _stream = await self._live_game(real_servicer, old_release)
        new_release = asyncio.Event()
        new_stream_opened = asyncio.Event()

        async def _new_stream(*_args, **_kwargs):
            new_stream_opened.set()
            await new_release.wait()

        async def _lookup_while_a_new_game_starts(*_args, **_kwargs):
            old_release.set()
            await old_task
            await real_servicer.start_game("s1", source="web", controllers=["s1", "s2"])
            await asyncio.wait_for(new_stream_opened.wait(), timeout=2)
            return game_coordinator_pb2.GetGameStateResponse(
                success=True, game_info=game_coordinator_pb2.GameInfo(state=game_coordinator_pb2.GameState.ENDED)
            )

        real_servicer.game_coordinator_channel.unary_unary.return_value = AsyncMock(
            side_effect=_lookup_while_a_new_game_starts
        )
        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True, side_effect=_new_stream):
            await real_servicer.ProcessInput(menu_pb2.ProcessInputRequest(input_type="reset_menu"), MagicMock())
            new_task = real_servicer._game_lifecycle_task
            assert new_task is not None
            assert new_task is not old_task
            assert not new_task.done()
            assert ("game_start_cancelled", {}) not in _published(real_servicer)
            # The new game still blocks a second start.
            await real_servicer.start_game("s2", source="admin_force_start", controllers=["s1", "s2"])
            assert self._rejection("admin_force_start") in _published(real_servicer)
            new_release.set()
            await new_task
        assert not new_task.cancelled()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A8", br=["BR-008"])
    async def test_start_during_reset_menu_lobby_restore_is_refused(self, real_servicer):
        """A start while reset_menu restores the lobby gets the A8 lobby-restart refusal (beep), then starts later."""
        from proto import game_coordinator_pb2

        never = asyncio.Event()
        task, _stream = await self._live_game(real_servicer, never)
        self._game_state(real_servicer, game_coordinator_pb2.GameState.ENDED)
        music_gate = asyncio.Event()
        in_window = asyncio.Event()

        async def _music_blocks():
            in_window.set()
            await music_gate.wait()

        real_servicer.audio.start_lobby_music.side_effect = _music_blocks
        reset = asyncio.create_task(
            real_servicer.ProcessInput(menu_pb2.ProcessInputRequest(input_type="reset_menu"), MagicMock())
        )
        await asyncio.wait_for(in_window.wait(), timeout=2)
        assert task.cancelled()
        restore = real_servicer._game_lifecycle_task
        assert restore is not None
        assert not restore.done()
        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as blocked:
            await real_servicer.start_game("s1", source="web", controllers=["s1", "s2"])
            blocked.assert_not_awaited()
        assert real_servicer._game_lifecycle_task is restore
        assert self._rejection("web") in _published(real_servicer)
        assert self._beeped(real_servicer)
        real_servicer.audio.start_lobby_music.side_effect = None
        music_gate.set()
        await reset
        assert real_servicer._game_lifecycle_task is None

        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as second:
            await real_servicer.start_game("s1", source="web", controllers=["s1", "s2"])
            await _await_lifecycle(real_servicer)
        second.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A8", br=["BR-008"])
    async def test_second_reset_menu_during_lobby_restore_keeps_every_controller(self, real_servicer):
        """A second reset_menu while state_manager.reset() re-registers is ignored; the first RPC ends normally."""
        from proto import game_coordinator_pb2

        never = asyncio.Event()
        await self._live_game(real_servicer, never)
        self._game_state(real_servicer, game_coordinator_pb2.GameState.ENDED)
        manager = real_servicer.state_manager
        original_connect = manager.on_controller_connected
        in_reset = asyncio.Event()
        reset_gate = asyncio.Event()

        async def _connect_blocking(serial, *args, **kwargs):
            if serial == "s2" and not reset_gate.is_set():
                in_reset.set()
                await reset_gate.wait()
            return await original_connect(serial, *args, **kwargs)

        manager.on_controller_connected = _connect_blocking
        request = menu_pb2.ProcessInputRequest(input_type="reset_menu")
        first = asyncio.create_task(real_servicer.ProcessInput(request, MagicMock()))
        await asyncio.wait_for(in_reset.wait(), timeout=2)
        assert real_servicer._lobby_restarting is True

        await real_servicer.ProcessInput(request, MagicMock())

        reset_gate.set()
        await first
        assert not first.cancelled()
        assert manager.connected_controllers == {"s1", "s2"}
        assert real_servicer._game_lifecycle_task is None
        assert real_servicer._lobby_restarting is False

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A8", br=["BR-008"])
    async def test_refusal_mid_game_after_a_lobby_restart_stays_silent(self, real_servicer):
        """The lobby-restart beep window closes when the restore finishes: a later mid-game refusal is silent."""
        await real_servicer._restart_lobby()
        assert real_servicer._lobby_restarting is False
        release = asyncio.Event()
        task, stream = await self._live_game(real_servicer, release)
        await real_servicer.start_game("s1", source="web", controllers=["s1", "s2"])
        await self._assert_refused(real_servicer, task, stream, "web", release)


class TestMenuServicerLifecycle:
    """Button monitor, game lifecycle task and shutdown on the real servicer."""

    @pytest.mark.asyncio
    async def test_button_monitor_delegates_to_controller_events(self, real_servicer):
        """start/stop_button_monitor and button_monitor_running delegate to the event loop."""
        await real_servicer.start_button_monitor()
        await real_servicer.stop_button_monitor()

        real_servicer.controller_events.start.assert_awaited_once()
        real_servicer.controller_events.stop.assert_awaited_once()
        real_servicer.controller_events.is_running = True
        assert real_servicer.button_monitor_running is True

    @pytest.mark.asyncio
    async def test_stop_game_event_monitor_cancels_lifecycle_task(self, real_servicer):
        """stop_game_event_monitor cancels a running game lifecycle task."""
        task = asyncio.create_task(asyncio.sleep(10))
        real_servicer._game_lifecycle_task = task

        await real_servicer.stop_game_event_monitor()

        assert task.cancelled()
        assert real_servicer._game_lifecycle_task is None

    @pytest.mark.asyncio
    async def test_start_game_keeps_running_lifecycle(self, real_servicer):
        """A _start_game while a lifecycle task is live refuses and leaves it running (#2054)."""
        old = asyncio.create_task(asyncio.sleep(10))
        real_servicer._game_lifecycle_task = old

        with patch.object(type(real_servicer), "_run_game_lifecycle", autospec=True) as lifecycle:
            await real_servicer._start_game("web", source="web")

        lifecycle.assert_not_called()
        assert not old.cancelled()
        assert real_servicer._game_lifecycle_task is old
        old.cancel()

    @pytest.mark.asyncio
    async def test_shutdown_stops_idle_monitor_and_closes_channels(self, real_servicer):
        """shutdown stops the idle monitor, cancels the lifecycle and closes the gRPC channels."""
        task = asyncio.create_task(asyncio.sleep(10))
        real_servicer._game_lifecycle_task = task

        await real_servicer.shutdown()

        real_servicer.idle_monitor.stop.assert_called_once()
        assert task.cancelled()
        # All three channels are the same autospec'd channel in this fixture.
        assert real_servicer.controller_channel.close.await_count == 3


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
    @pytest.mark.uc("UC-010", flow="A3", steps=[1])
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

    @pytest.mark.uc("UC-010", flow="main", steps=[5])
    def test_build_config_ffa(self, servicer, players):
        """JoustFFA should produce ffa_config with correct base fields."""
        config = self._build_config(servicer, Games.JoustFFA, players)

        assert config.game_name == "JoustFFA"
        assert config.sensitivity == 2
        assert len(config.players) == 2
        assert config.HasField("ffa_config")

    @pytest.mark.uc("UC-010", flow="main", steps=[5])
    def test_build_config_marks_menu_origin(self, servicer, players):
        """The menu is the REAL game (#837): its config carries GAME_ORIGIN_MENU
        so the coordinator reserves the primary slot for it."""
        from proto import game_coordinator_pb2

        config = self._build_config(servicer, Games.JoustFFA, players)
        assert config.origin == game_coordinator_pb2.GAME_ORIGIN_MENU

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
    @pytest.mark.uc("UC-010", flow="main", steps=[8])
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
    @pytest.mark.uc("UC-010", flow="main", steps=[8])
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
    @pytest.mark.uc("UC-010", flow="main", steps=[8])
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
    @pytest.mark.uc("UC-010", flow="main", steps=[8])
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
    @pytest.mark.uc("UC-010", flow="main", steps=[8])
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
    @pytest.mark.uc("UC-010", flow="main", steps=[8])
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


class TestRestartLobbyRestoresLobby:
    """UC-010 step 8 through the real servicer and StateManager (only I/O edges autospec'd)."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="main", steps=[8])
    async def test_every_player_back_to_connected_with_lobby_colour_and_music(self, real_servicer):
        """After the game: READY players are un-readied (CONNECTED, lobby LED), lobby music and idle monitor on."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        await _connect(real_servicer, "s1", "s2", "s3")
        await _ready_up(real_servicer, "s1", "s2")  # s3 stays CONNECTED: no auto start
        assert real_servicer.state_manager.ready_controllers == {"s1", "s2"}
        real_servicer.state = menu_pb2.MenuState.GAME_STARTING
        led = real_servicer.state_manager.led
        led.reset_mock()

        await real_servicer._restart_lobby()

        assert real_servicer.state == menu_pb2.MenuState.RUNNING
        assert real_servicer.state_manager.controller_states == {
            "s1": ControllerState.CONNECTED,
            "s2": ControllerState.CONNECTED,
            "s3": ControllerState.CONNECTED,
        }
        assert real_servicer.state_manager.ready_controllers == set()
        lobby_colours = sorted(c.args for c in led.set_connected_color.await_args_list)
        assert lobby_colours == [("s1", "JoustFFA"), ("s2", "JoustFFA"), ("s3", "JoustFFA")]
        real_servicer.audio.start_lobby_music.assert_awaited_once()
        real_servicer.idle_monitor.start.assert_called_once()


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

    @pytest.mark.uc("UC-010", flow="main", steps=[5])
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

    @pytest.mark.uc("UC-010", flow="main", steps=[5])
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

    @pytest.mark.uc("UC-010", flow="main", steps=[5])
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

    @pytest.mark.uc("UC-010", flow="main", steps=[5])
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

    @pytest.mark.uc("UC-010", flow="main", steps=[5])
    def test_werewolf_custom_reveal_time(self, servicer, players):
        """Werewolf should use reveal_time_seconds from flagd."""
        servicer.game_settings_client.get_float_value = MagicMock(
            side_effect=lambda key, default: {
                "werewolf.reveal_time_seconds": 60.0,
            }.get(key, default)
        )
        config = self._build_config(servicer, Games.Werewolf, players)
        assert config.werewolf_config.reveal_time_seconds == pytest.approx(60.0)

    @pytest.mark.uc("UC-010", flow="main", steps=[5])
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
    @pytest.mark.uc("UC-027", flow="main", steps=[5])
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
    @pytest.mark.uc("UC-010", flow="main", steps=[5], br=["BR-006"])
    async def test_game_start_first_confirms_the_start_once(self, servicer):
        """game_start is the first event of a menu start stream (#1783): it confirms the start.

        The coordinator yields it only after the game thread was started, so the
        menu clears the ready state and marks the game started on it, exactly once;
        the following game_starting is an ordinary event.
        """
        from services.menu.servicer import MenuServicer

        types = ["game_start", "game_starting", "game_started", "game_ended"]
        events = []
        for event_type in types:
            event = MagicMock()
            event.event_type = event_type
            event.data = {"game_id": "game_abc"}
            events.append(event)

        async def fake_stream():
            for event in events:
                yield event

        span = MagicMock()
        result = await MenuServicer._consume_game_events(servicer, fake_stream(), False, span)

        assert result.game_started is True
        assert result.should_return is True
        servicer._clear_ready_state.assert_called_once()
        span.set_attribute.assert_called_once_with("game.started", True)
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
    @pytest.mark.uc("UC-010", flow="A3", steps=[1])
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


class TestRunGameLifecycleMidStartFailure:
    """Tests for _run_game_lifecycle recovery when prep/build throws.

    _prepare_game_start() sets state=GAME_STARTING and tears down the lobby. If
    anything between there and the protected _run_game_event_stream throws (bad
    mode config, stub creation, audio/button RPC error), the menu must not get
    stuck in GAME_STARTING with no lobby — it should restore the lobby and reset
    to RUNNING, then re-raise so the failure still surfaces.
    """

    @pytest.fixture
    def servicer(self):
        s = FakeMenuServicer()
        # _run_game_lifecycle + _prepare_game_start + _restart_lobby dependencies
        s.idle_monitor = MagicMock()
        s.stop_button_monitor = AsyncMock()
        s.start_button_monitor = AsyncMock()
        # Two ready controllers so the roster check passes and we reach the
        # prep/build window we want to exercise.
        s.state_manager.controller_states = {
            "s1": ControllerState.READY,
            "s2": ControllerState.READY,
        }
        return s

    async def _run(self, servicer):
        """Call the real _run_game_lifecycle bound to the fake servicer."""
        from unittest.mock import patch

        from services.menu.servicer import MenuServicer

        with patch("services.menu.servicer.metrics"):
            await MenuServicer._run_game_lifecycle(servicer, "s1", source="web")

    @pytest.mark.asyncio
    async def test_build_config_exception_restores_lobby(self, servicer):
        """An exception in _build_game_config restores the lobby and re-raises."""
        from services.menu.servicer import MenuServicer

        # Bind the real prep/restore helpers so we exercise the actual teardown +
        # restore, but make config-building blow up mid-start.
        servicer._prepare_game_start = lambda: MenuServicer._prepare_game_start(servicer)
        servicer._restart_lobby = lambda: MenuServicer._restart_lobby(servicer)
        servicer._build_trace_metadata = MagicMock(return_value=[])
        servicer._build_game_config = MagicMock(side_effect=RuntimeError("bad mode config"))

        with pytest.raises(RuntimeError, match="bad mode config"):
            await self._run(servicer)

        # Lobby restored, not stuck in GAME_STARTING.
        assert servicer.state == menu_pb2.MenuState.RUNNING
        servicer.start_button_monitor.assert_awaited()
        servicer.audio.start_lobby_music.assert_awaited()
        servicer.state_manager.reset.assert_awaited()

    @pytest.mark.asyncio
    async def test_no_recovery_when_not_game_starting(self, servicer):
        """If the failure happens after the lobby was already restored (state no
        longer GAME_STARTING), we don't double-restore — but we still re-raise."""
        from services.menu.servicer import MenuServicer

        servicer._prepare_game_start = lambda: MenuServicer._prepare_game_start(servicer)
        restart = AsyncMock()
        servicer._restart_lobby = restart
        servicer._build_trace_metadata = MagicMock(return_value=[])

        def build_then_flip(*_args, **_kwargs):
            # Simulate the stream already having restored the lobby.
            servicer.state = menu_pb2.MenuState.RUNNING
            raise RuntimeError("late failure")

        servicer._build_game_config = MagicMock(side_effect=build_then_flip)

        with pytest.raises(RuntimeError, match="late failure"):
            await self._run(servicer)

        # State already RUNNING, so the guard skips the redundant restore.
        restart.assert_not_awaited()


class TestRunGameLifecycleRosterAbort:
    """UC-010 A3: a start aborted for a short roster publishes game_start_cancelled.

    Calls the real MenuServicer._run_game_lifecycle bound to the fake servicer.
    """

    @pytest.fixture
    def servicer(self):
        s = FakeMenuServicer()
        s.idle_monitor = MagicMock()
        s._prepare_game_start = AsyncMock()
        return s

    async def _run(self, servicer, controllers=None):
        from unittest.mock import patch

        from services.menu.servicer import MenuServicer

        with patch("services.menu.servicer.metrics"), patch("services.menu.servicer.asyncio.sleep", AsyncMock()):
            await MenuServicer._run_game_lifecycle(servicer, "s1", source="controller", controllers=controllers)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A3")
    async def test_ready_path_short_roster_publishes_cancelled(self, servicer):
        """Ready/auto-start path (menu still RUNNING) publishes game_start_cancelled."""
        servicer.state = menu_pb2.MenuState.RUNNING
        servicer.state_manager.controller_states = {"s1": ControllerState.READY, "s2": ControllerState.CONNECTED}

        await self._run(servicer)

        servicer.event_publisher.publish.assert_awaited_once_with(
            "game_start_cancelled", {"game_name": "JoustFFA", "player_count": 1, "minimum_players": 2}
        )
        assert servicer.state == menu_pb2.MenuState.RUNNING
        servicer._prepare_game_start.assert_not_awaited()
        servicer.state_manager.reset.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A3")
    async def test_force_start_short_roster_resets_state_and_publishes_once(self, servicer):
        """Admin force-start path (GAME_STARTING) returns to RUNNING and publishes exactly once."""
        servicer.state = menu_pb2.MenuState.GAME_STARTING

        await self._run(servicer, controllers=["s1"])

        servicer.event_publisher.publish.assert_awaited_once_with(
            "game_start_cancelled", {"game_name": "JoustFFA", "player_count": 1, "minimum_players": 2}
        )
        assert servicer.state == menu_pb2.MenuState.RUNNING


class TestHandleWebCommandSelectGame:
    """UC-011 A1: web select_game continues at step 5 (both selection events), on the real servicer."""

    @staticmethod
    async def _select(servicer, game_name):
        request = menu_pb2.ProcessInputRequest(
            input_type="web_command", data={"command": "select_game", "game_name": game_name}
        )
        await servicer.ProcessInput(request, MagicMock())

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="A1", steps=[5])
    async def test_web_select_publishes_both_selection_events(self, real_servicer):
        await self._select(real_servicer, "Tournament")

        published = _published(real_servicer)
        assert ("selection_changed", {"game_name": "Tournament", "source": "web"}) in published
        assert ("game_selection_changed", {"game_name": "Tournament", "source": "web"}) in published
        assert real_servicer.current_selection == Games.Tournament
        assert real_servicer.state_manager.current_game_mode == Games.Tournament
        real_servicer.user_prefs_writer.update_default_variant.assert_called_once_with("current_game", "Tournament")
        real_servicer.audio.play_voice.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_web_select_persists_off_the_event_loop_thread(self, real_servicer):
        """The blocking flag-service write runs in a worker thread, not on the loop."""
        import threading

        loop_thread = threading.get_ident()
        write_threads = []
        real_servicer.user_prefs_writer.update_default_variant.side_effect = lambda *_a: write_threads.append(
            threading.get_ident()
        )

        await self._select(real_servicer, "Tournament")

        assert len(write_threads) == 1
        assert write_threads[0] != loop_thread

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="A1")
    async def test_web_select_unknown_game_publishes_nothing(self, real_servicer):
        await self._select(real_servicer, "NotAGame")

        real_servicer.event_publisher.publish.assert_not_awaited()
        assert real_servicer.current_selection == Games.JoustFFA
        real_servicer.user_prefs_writer.update_default_variant.assert_not_called()
        real_servicer.audio.play_voice.assert_not_awaited()


class TestSelectionPathsShareSelectGameMode:
    """#1285: every game-mode selection path goes through MenuServicer.select_game_mode."""

    @staticmethod
    async def _admin_cross(servicer):
        await servicer.admin_handler.handle_game_mode("host")

    @staticmethod
    async def _lobby_select(servicer):
        await _connect(servicer, "s1")
        await servicer.on_button("s1", "select", True)

    @staticmethod
    async def _process_input_select(servicer):
        request = menu_pb2.ProcessInputRequest(input_type="button_press", data={"button": "select"})
        await servicer.ProcessInput(request, MagicMock())

    @staticmethod
    async def _web_select(servicer):
        request = menu_pb2.ProcessInputRequest(
            input_type="web_command", data={"command": "select_game", "game_name": "JoustTeams"}
        )
        await servicer.ProcessInput(request, MagicMock())

    @staticmethod
    async def _web_start_with_mode(servicer):
        request = menu_pb2.ProcessInputRequest(
            input_type="web_command", data={"command": "start_game", "mode": "JoustTeams"}
        )
        with patch.object(type(servicer), "_start_game", autospec=True):
            await servicer.ProcessInput(request, MagicMock())

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="main", steps=[4, 5])
    @pytest.mark.parametrize(
        ("path", "source"),
        [
            ("_admin_cross", "admin"),
            ("_lobby_select", "controller"),
            ("_process_input_select", "button_input"),
            ("_web_select", "web"),
            ("_web_start_with_mode", "web"),
        ],
    )
    async def test_selection_path_routes_through_select_game_mode(self, real_servicer, path, source):
        real_servicer.state = menu_pb2.MenuState.RUNNING
        select_spy = AsyncMock(wraps=real_servicer.select_game_mode)

        with patch.object(real_servicer, "select_game_mode", select_spy):
            # The state manager and admin handler hold the bound method from __init__.
            real_servicer.state_manager._select_game_mode = select_spy
            await getattr(self, path)(real_servicer)

        select_spy.assert_awaited_once()
        assert select_spy.await_args.kwargs.get("source", "admin") == source
        assert real_servicer.current_selection == Games.JoustTeams
        assert real_servicer.state_manager.current_game_mode == Games.JoustTeams
        real_servicer.user_prefs_writer.update_default_variant.assert_called_once_with("current_game", "JoustTeams")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="main", steps=[4, 5])
    async def test_admin_cross_persists_and_publishes_both_events(self, real_servicer):
        """Admin Cross through the real servicer: persisted, selection_changed and game_selection_changed."""
        await self._admin_cross(real_servicer)

        published = _published(real_servicer)
        assert ("selection_changed", {"game_name": "JoustTeams", "source": "admin"}) in published
        assert (
            "game_selection_changed",
            {"game_name": "JoustTeams", "source": "admin_mode", "serial": "host"},
        ) in published
        real_servicer.user_prefs_writer.update_default_variant.assert_called_once_with("current_game", "JoustTeams")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="main", steps=[6])
    async def test_admin_cross_leaves_other_lobby_leds_alone(self, real_servicer):
        """UC-011 step 6: Cross changes the mode, but no other lobby controller's LED is touched.

        The only LED output of the selection is the pulse on the host's own controller;
        the CONNECTED controllers keep their state and colour until they next enter CONNECTED.
        """
        real_servicer.state = menu_pb2.MenuState.RUNNING
        await _connect(real_servicer, "s1", "s2", "s3")
        led = real_servicer.state_manager.led
        assert led.set_connected_color.await_count == 3  # lobby colours were set on connect
        led.reset_mock()

        await self._admin_cross(real_servicer)

        assert real_servicer.current_selection == Games.JoustTeams
        touched = {c.args[0] for c in led.mock_calls if c.args}
        assert touched == {"host"}
        led.send_game_effect.assert_awaited_once()
        assert {real_servicer.state_manager.get_controller_state(s) for s in ("s1", "s2", "s3")} == {
            ControllerState.CONNECTED
        }

        # The new colour shows the next time a controller enters CONNECTED (here: READY -> Move un-ready).
        await _ready_up(real_servicer, "s1")
        led.reset_mock()
        await real_servicer.on_button("s1", "move", True)
        led.set_connected_color.assert_awaited_once_with("s1", Games.JoustTeams.name)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="A2", steps=[3])
    async def test_lobby_select_event_carries_source_and_serial_without_game_selection_changed(self, real_servicer):
        """UC-011 A2 step 3: lobby SELECT publishes selection_changed (source, serial), not game_selection_changed."""
        real_servicer.state = menu_pb2.MenuState.RUNNING

        await self._lobby_select(real_servicer)

        published = _published(real_servicer)
        assert (
            published.count(("selection_changed", {"game_name": "JoustTeams", "source": "controller", "serial": "s1"}))
            == 1
        )
        assert [name for name, _ in published if name == "game_selection_changed"] == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="A2", steps=[5])
    async def test_lobby_select_colour_shows_when_controller_next_enters_connected(self, real_servicer):
        """UC-011 A2 step 5: after lobby SELECT, a controller entering CONNECTED shows the new mode's colour."""
        real_servicer.state = menu_pb2.MenuState.RUNNING
        await _connect(real_servicer, "s1", "s2")
        led = real_servicer.state_manager.led
        await real_servicer.on_button("s1", "select", True)
        assert real_servicer.current_selection == Games.JoustTeams
        led.reset_mock()

        # s2 readies up and un-readies: it re-enters CONNECTED after the selection.
        await _ready_up(real_servicer, "s2")
        await real_servicer.on_button("s2", "move", True)

        led.set_connected_color.assert_awaited_once_with("s2", Games.JoustTeams.name)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="main", steps=[3])
    async def test_admin_cross_pulses_host_and_plays_the_mode_voice_line(self, real_servicer):
        """UC-011 step 3: Cross pulses the host's controller and plays the selected mode's voice line."""
        await self._admin_cross(real_servicer)

        real_servicer.audio.play_game_mode_voice.assert_awaited_once_with(Games.JoustTeams.name)
        real_servicer.state_manager.led.send_game_effect.assert_awaited_once_with(
            "host",
            controller_manager_pb2.GAME_EFFECT_PULSE,
            color=GAME_MODE_COLORS[Games.JoustTeams.name],
            duration_ms=600,
            speed=6,
        )

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="main")  # Post-F-1: the previous selection stays
    async def test_select_unknown_mode_changes_nothing(self, real_servicer):
        real_servicer.current_selection = Games.Tournament
        real_servicer.state_manager.set_game_mode(Games.Tournament)

        await real_servicer.select_game_mode("NotAMode", source="admin")

        assert real_servicer.current_selection == Games.Tournament
        assert real_servicer.state_manager.current_game_mode == Games.Tournament
        real_servicer.event_publisher.publish.assert_not_awaited()
        real_servicer.user_prefs_writer.update_default_variant.assert_not_called()


class TestIdleWakeThenReadyUp:
    """UC-010 A2 step 3 on the real servicer: after waking from idle the lobby continues at step 1."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A2", steps=[3])
    @patch("services.menu.idle_monitor.metrics")
    async def test_ready_up_works_after_wake_from_idle(self, _mock_metrics, real_servicer):
        """Idle, wake with a press (consumed), then trigger presses ready up and start the game."""
        from services.menu.idle_monitor import IdleMonitor

        real_servicer.state = menu_pb2.MenuState.RUNNING
        monitor = IdleMonitor(
            state_manager=real_servicer.state_manager,
            get_idle_enabled=lambda: True,
            get_idle_timeout=lambda: 15,
            get_sentinel_count=lambda: 0,
            get_rotation_minutes=lambda: 5,
        )
        real_servicer.idle_handler.set_wake_callback(monitor.wake_all)
        await _connect(real_servicer, "s1", "s2")
        await monitor._enter_idle_mode()
        states = real_servicer.state_manager.controller_states
        assert set(states.values()) == {ControllerState.IDLE}

        await real_servicer.on_button("s1", "trigger", True)  # the waking press is consumed
        await real_servicer.on_button("s1", "trigger", False)
        assert set(states.values()) == {ControllerState.CONNECTED}
        assert real_servicer.ready_controllers == set()

        with patch.object(type(real_servicer), "_run_game_event_stream", autospec=True) as stream:
            await _ready_up(real_servicer, "s1", "s2")
            await _await_lifecycle(real_servicer)

        assert _started_roster(stream) == ["s1", "s2"]
        assert real_servicer.state == menu_pb2.MenuState.GAME_STARTING


class _ReconnectStub:
    """Fake coordinator stub for UC-010 A9: the start stream breaks after game_start, later
    streams are primary-bus reconnects, and GetGameState answers with ``state`` / ``game_id``.

    ``failed_reconnects`` reconnect attempts fail like a coordinator that is still down: grpc.aio
    returns empty headers for an UNAVAILABLE call and the error only surfaces on iteration, and
    GetGameState fails too. ``lookup_failure`` ("raise" | "unsuccessful") makes the state lookup of
    a healthy reconnect fail; ``ended_after_check`` makes a reconnect stream deliver ``game_ended``
    once the state was asked.
    """

    def __init__(self, state, game_id, *, failed_reconnects=0, ended_after_check=False, lookup_failure=None):
        self.state = state
        self.game_id = game_id
        self.failed_reconnects = failed_reconnects
        self.ended_after_check = ended_after_check
        self.lookup_failure = lookup_failure
        self._down = False
        self.requests = []
        self.state_requests = []
        self.headers_before_check = []
        self._headers_received = False
        self._checked = asyncio.Event()

    def __call__(self, _channel):
        return self

    def StreamGameEvents(self, request, metadata=None):  # noqa: N802 - gRPC stub name
        from proto import game_coordinator_pb2

        self.requests.append(request)
        stub = self

        if request.HasField("start_config"):

            class Start:
                def cancel(self):
                    pass

                async def _events(self):
                    yield game_coordinator_pb2.GameEvent(event_type="game_start", game_id="g1")
                    raise ConnectionError("stream broke")

                def __aiter__(self):
                    return self._events()

            return Start()

        stub._down = stub.failed_reconnects > 0
        if stub._down:
            stub.failed_reconnects -= 1
        down = stub._down

        class Reconnect:
            def cancel(self):
                pass

            async def initial_metadata(self):
                if not down:
                    stub._headers_received = True
                return ()

            async def _events(self):
                if down:
                    raise ConnectionError("coordinator unavailable")
                await stub._checked.wait()
                if stub.ended_after_check:
                    yield game_coordinator_pb2.GameEvent(event_type="game_ended", game_id="g1", data={"winner": "s1"})
                await asyncio.Event().wait()  # an open stream that never delivers

            def __aiter__(self):
                return self._events()

        return Reconnect()

    async def GetGameState(self, request, **_kwargs):  # noqa: N802 - gRPC stub name
        from proto import game_coordinator_pb2

        if self._down:
            raise ConnectionError("coordinator unavailable")
        self.state_requests.append(request)
        self.headers_before_check.append(self._headers_received)
        self._checked.set()
        if self.lookup_failure == "raise":
            raise ConnectionError("GetGameState failed")
        if self.lookup_failure == "unsuccessful":
            return game_coordinator_pb2.GetGameStateResponse(success=False, error="not ready")
        return game_coordinator_pb2.GetGameStateResponse(
            success=True,
            game_info=game_coordinator_pb2.GameInfo(state=self.state, game_id=self.game_id),
        )


class TestReconnectChecksFollowedGame:
    """UC-010 A9 / BR-009 (#2063): after a stream reconnect the menu checks that its game still exists."""

    @staticmethod
    async def _run(real_servicer, stub):
        real_servicer.state = menu_pb2.MenuState.RUNNING
        await _connect(real_servicer, "s1", "s2")
        real_servicer.audio.start_lobby_music.reset_mock()
        real_servicer.idle_monitor.start.reset_mock()
        # UC-010 A8 lobby-restart window: the restore runs with _lobby_restarting set.
        restarting_seen = []
        real_servicer.audio.start_lobby_music.side_effect = lambda: restarting_seen.append(
            real_servicer._lobby_restarting
        )
        real_servicer.restarting_seen = restarting_seen
        real_sleep = asyncio.sleep

        async def fast_sleep(_delay, *_args, **_kwargs):
            await real_sleep(0)

        with (
            patch("proto.game_coordinator_pb2_grpc.GameCoordinatorServiceStub", stub),
            patch("services.menu.servicer.asyncio.sleep", fast_sleep),
        ):
            await _ready_up(real_servicer, "s1", "s2")
            await asyncio.wait_for(_await_lifecycle(real_servicer), timeout=5)

    @staticmethod
    def _assert_lobby_restored(servicer):
        assert servicer.state == menu_pb2.MenuState.RUNNING
        assert servicer.state_manager.controller_states == {
            "s1": ControllerState.CONNECTED,
            "s2": ControllerState.CONNECTED,
        }
        servicer.audio.start_lobby_music.assert_awaited_once()
        servicer.idle_monitor.start.assert_called_once()
        assert servicer.restarting_seen == [True]
        assert servicer._lobby_restarting is False

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A9", steps=[1, 2, 4], br=["BR-006", "BR-009"])
    @pytest.mark.parametrize(
        ("state_name", "active_game_id"),
        [("ENDED", "g1"), ("IDLE", ""), ("RUNNING", "g2"), ("STARTING", "g2")],
    )
    async def test_reconnect_to_ended_game_finishes_lifecycle(self, real_servicer, state_name, active_game_id):
        """The game ended while the stream was down (or a different game now runs): the lifecycle
        publishes game_ended for the followed game, restores the lobby and finishes."""
        from proto import game_coordinator_pb2

        stub = _ReconnectStub(game_coordinator_pb2.GameState.Value(state_name), active_game_id)
        await self._run(real_servicer, stub)

        assert not stub.requests[1].HasField("start_config")  # BR-006: plain primary-bus subscribe
        assert not stub.requests[1].game_id
        assert stub.headers_before_check == [True]  # checked only once the subscription is registered
        assert not stub.state_requests[0].game_id  # primary game
        assert ("game_ended", {"game_id": "g1", "reason": "ended_while_disconnected"}) in _published(real_servicer)
        self._assert_lobby_restored(real_servicer)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A9", steps=[1, 2, 4], br=["BR-009"])
    async def test_reconnect_after_coordinator_restart_finishes_lifecycle(self, real_servicer):
        """The coordinator restarted: the first reconnect fails, the next one finds no game (IDLE)."""
        from proto import game_coordinator_pb2

        stub = _ReconnectStub(game_coordinator_pb2.GameState.IDLE, "", failed_reconnects=1)
        await self._run(real_servicer, stub)

        assert len(stub.requests) == 3  # start stream, failed reconnect, reconnect
        assert stub.headers_before_check == [True]
        assert ("game_ended", {"game_id": "g1", "reason": "ended_while_disconnected"}) in _published(real_servicer)
        self._assert_lobby_restored(real_servicer)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A9", steps=[1, 3, 4], br=["BR-009"])
    async def test_reconnect_to_running_game_continues(self, real_servicer):
        """The followed game still runs: the lifecycle keeps the reconnected stream and ends on the
        game's own game_ended."""
        from proto import game_coordinator_pb2

        stub = _ReconnectStub(game_coordinator_pb2.GameState.RUNNING, "g1", ended_after_check=True)
        await self._run(real_servicer, stub)

        assert stub.headers_before_check == [True]
        published = _published(real_servicer)
        assert ("game_ended", {"winner": "s1"}) in published
        assert not [d for e, d in published if e == "game_ended" and d.get("reason") == "ended_while_disconnected"]
        self._assert_lobby_restored(real_servicer)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A9", steps=[1, 3], br=["BR-009", "BR-008"])
    @pytest.mark.parametrize("lookup_failure", ["raise", "unsuccessful"])
    async def test_failed_state_lookup_keeps_the_stream(self, real_servicer, lookup_failure):
        """GetGameState fails or answers unsuccessfully: the menu does not guess the game is gone.
        No game_ended is published, the lobby is not restored and the lifecycle keeps its stream."""
        from proto import game_coordinator_pb2

        stub = _ReconnectStub(game_coordinator_pb2.GameState.IDLE, "", lookup_failure=lookup_failure)
        real_servicer.state = menu_pb2.MenuState.RUNNING
        await _connect(real_servicer, "s1", "s2")
        real_servicer.audio.start_lobby_music.reset_mock()
        real_sleep = asyncio.sleep

        async def fast_sleep(_delay, *_args, **_kwargs):
            await real_sleep(0)

        with (
            patch("proto.game_coordinator_pb2_grpc.GameCoordinatorServiceStub", stub),
            patch("services.menu.servicer.asyncio.sleep", fast_sleep),
        ):
            await _ready_up(real_servicer, "s1", "s2")
            task = real_servicer._game_lifecycle_task
            try:
                for _ in range(200):
                    if stub.state_requests:
                        break
                    await real_sleep(0.01)
                assert stub.state_requests, "the reconnect never asked for the game state"
                for _ in range(20):  # let a wrong "gone" verdict play out
                    await real_sleep(0)
                assert not task.done()
                assert len(stub.requests) == 2  # start stream and one reconnect, no retry
                assert not [d for e, d in _published(real_servicer) if d.get("reason") == "ended_while_disconnected"]
                real_servicer.audio.start_lobby_music.assert_not_awaited()
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A9", steps=[1, 2, 4], br=["BR-006", "BR-009"])
    async def test_followed_game_id_is_reset_per_lifecycle(self, real_servicer):
        """A second lifecycle that breaks before its first event is an A3 restore: it must not
        reconnect nor publish game_ended for the previous lifecycle's game id."""
        from proto import game_coordinator_pb2

        stub = _ReconnectStub(game_coordinator_pb2.GameState.IDLE, "")
        await self._run(real_servicer, stub)
        assert len(stub.requests) == 2
        real_servicer.audio.start_lobby_music.reset_mock()
        real_servicer.event_publisher.publish.reset_mock()
        stub.requests.clear()

        def breaks_before_first_event(request, metadata=None):  # noqa: ARG001
            stub.requests.append(request)

            class Broken:
                def cancel(self):
                    pass

                async def _events(self):
                    raise ConnectionError("stream broke before the first event")
                    yield  # pragma: no cover - makes this an async generator

                def __aiter__(self):
                    return self._events()

            return Broken()

        stub.StreamGameEvents = breaks_before_first_event
        real_sleep = asyncio.sleep

        async def fast_sleep(_delay, *_args, **_kwargs):
            await real_sleep(0)

        real_servicer.state = menu_pb2.MenuState.RUNNING
        with (
            patch("proto.game_coordinator_pb2_grpc.GameCoordinatorServiceStub", stub),
            patch("services.menu.servicer.asyncio.sleep", fast_sleep),
        ):
            await real_servicer._start_game("s1", controllers=["s1", "s2"])
            await asyncio.wait_for(_await_lifecycle(real_servicer), timeout=5)

        assert len(stub.requests) == 1  # no reconnect
        assert not [d for e, d in _published(real_servicer) if d.get("reason") == "ended_while_disconnected"]
        real_servicer.audio.start_lobby_music.assert_awaited_once()
