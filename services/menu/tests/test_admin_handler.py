"""Unit tests for AdminModeHandler game settings."""

import asyncio
import logging
import threading
import time
from unittest.mock import AsyncMock, MagicMock, call, create_autospec, patch

import grpc
import pytest
from opentelemetry import trace

from lib.colors import Colors
from lib.controller_constants import ButtonTrackingKey
from lib.flag_service_writer import FlagServiceWriter
from lib.types import Sound
from proto import menu_pb2
from services.menu.handlers.admin import AdminModeCallbacks, AdminModeHandler
from services.menu.handlers.base import ControllerHandler, ControllerState
from services.menu.idle_monitor import IdleMonitor
from services.menu.state_manager import StateManager
from services.menu.utils import AudioHelper, LedController


@pytest.fixture
def mock_tracer():
    """Create mock tracer."""
    tracer = create_autospec(trace.Tracer, instance=True)
    tracer.start_span.return_value = MagicMock()
    tracer.start_as_current_span.return_value = MagicMock(__enter__=MagicMock(), __exit__=MagicMock())
    return tracer


@pytest.fixture
def mock_callbacks():
    """Create mock callbacks."""
    callbacks = create_autospec(AdminModeCallbacks, instance=True)
    callbacks.get_game_options.return_value = ["JoustFFA", "JoustTeams"]
    callbacks.start_game.return_value = True
    return callbacks


@pytest.fixture
def mock_metrics():
    """Create mock metrics."""
    metrics = MagicMock()
    metrics.button_presses_total = MagicMock()
    metrics.button_presses_total.labels = MagicMock(return_value=MagicMock())
    return metrics


SENSITIVITY_VALUES = {"ultra_slow": 0, "slow": 1, "medium": 2, "fast": 3, "ultra_fast": 4}
AGENT_LEVEL_VALUES = {
    "none": "",
    "ambient": "a",
    "standard": "a,b",
    "full": "a,b,c",
    "probe": "p",
    "shadow_experimental": "s",
}


def flag_view(values: dict, effective_default: str) -> dict:
    """A flag-service GET body for one flag."""
    return {"variants": list(values), "variant_values": dict(values), "effective_default": effective_default}


def set_agent_level(manager, level: str) -> None:
    """Make both the flag service and flagd report ``level`` as the current agent policy."""
    manager.agent_settings_writer.get_flag_view.return_value = flag_view(AGENT_LEVEL_VALUES, level)
    manager.agent_settings_client.get_string_value.return_value = AGENT_LEVEL_VALUES[level]


@pytest.fixture
def mock_state_manager():
    """Create mock StateManager with FlagServiceWriter mocks."""
    manager = create_autospec(StateManager, instance=True)

    # FlagServiceWriter mocks for game_settings
    game_writer = create_autospec(FlagServiceWriter, instance=True)
    game_writer.get_current_variant = MagicMock(return_value="medium")
    game_writer.cycle_variant = MagicMock(return_value="fast")
    game_writer.get_variant_names = MagicMock(return_value=["ultra_slow", "slow", "medium", "fast", "ultra_fast"])
    manager.game_settings_writer = game_writer

    # Effective-value resolution (#818): one flag-service read per display.
    game_writer.get_flag_view = MagicMock(return_value=flag_view(SENSITIVITY_VALUES, "medium"))

    # FlagServiceWriter mocks for user_preferences
    user_writer = create_autospec(FlagServiceWriter, instance=True)
    user_writer.get_current_variant = MagicMock(return_value="on")
    user_writer.cycle_variant = MagicMock(return_value="off")
    manager.user_prefs_writer = user_writer

    # FlagServiceWriter + client mocks for agent policy (#819 kill-switch).
    agent_writer = create_autospec(FlagServiceWriter, instance=True)
    agent_writer.get_flag_view = MagicMock(return_value=flag_view(AGENT_LEVEL_VALUES, "ambient"))
    agent_writer.update_default_variant = MagicMock(return_value=True)
    manager.agent_settings_writer = agent_writer

    # Agent-level client mock (#819 level read).

    agent_client = MagicMock()
    agent_client.get_string_value = MagicMock(return_value=AGENT_LEVEL_VALUES["ambient"])
    manager.agent_settings_client = agent_client

    manager.led = create_autospec(LedController, instance=True)
    manager.led.send_game_effect.return_value = True
    manager.led.send_base_color.return_value = True
    manager.current_game_mode = MagicMock()
    manager.current_game_mode.name = "JoustFFA"
    manager.audio = create_autospec(AudioHelper, instance=True)
    manager.publish_event = AsyncMock()
    return manager


@pytest.fixture
def handler(mock_tracer, mock_callbacks, mock_metrics, mock_state_manager):
    """Create AdminModeHandler instance with mocks (pre-activated)."""
    handler = AdminModeHandler(
        controller_channel=create_autospec(grpc.aio.Channel, instance=True),
        tracer=mock_tracer,
        callbacks=mock_callbacks,
        metrics=mock_metrics,
    )
    handler.set_state_manager(mock_state_manager)
    handler.active = True
    handler.controller_serial = "test_serial"
    handler.entry_time = time.time()
    return handler


@pytest.fixture
def inactive_handler(mock_tracer, mock_callbacks, mock_metrics, mock_state_manager):
    """Create AdminModeHandler instance that is NOT pre-activated."""
    handler = AdminModeHandler(
        controller_channel=create_autospec(grpc.aio.Channel, instance=True),
        tracer=mock_tracer,
        callbacks=mock_callbacks,
        metrics=mock_metrics,
    )
    handler.set_state_manager(mock_state_manager)
    return handler


class TestAdminOptionNavigation:
    """Tests for admin option navigation."""

    @pytest.mark.uc("UC-012", flow="main", steps=[3], br=["BR-002"])
    def test_option_names_defined(self, handler):
        """Controller-cycled surface: the 3 human essentials (issue #815) plus
        the agent kill-switch (issue #819).

        The 5 per-mode tunables (random_assignment, nonstop.time_limit_seconds,
        invincibility_seconds, fight_club.min_rounds, werewolf.reveal_time_seconds)
        are intentionally NOT cycled on the controller anymore — they remain
        settable via their game.json flags (dashboard/file/flagd).
        """
        expected = [
            "sensitivity",
            "num_teams",
            "force_all_start",
            "agent_control",
        ]
        assert handler.option_names == expected

    @pytest.mark.uc("UC-012", flow="main", steps=[3], br=["BR-002"])
    def test_moved_options_not_cycled(self, handler):
        """The 5 moved-to-flag-only options must NOT be in the controller cycle."""
        moved = {
            "random_assignment",
            "nonstop.time_limit_seconds",
            "invincibility_seconds",
            "fight_club.min_rounds",
            "werewolf.reveal_time_seconds",
        }
        assert moved.isdisjoint(handler.option_names)

    @pytest.mark.uc("UC-012", flow="main", steps=[3])
    def test_option_colors_match_names(self, handler):
        """Each option should have a corresponding color."""
        assert len(handler.option_colors) == len(handler.option_names)

    def test_option_colors_are_colors_enum(self, handler):
        """Option colors should be Colors enum values."""
        for color in handler.option_colors:
            assert isinstance(color, Colors)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[3])
    async def test_cycle_option_increments(self, handler):
        """handle_cycle_option should increment current_option."""
        handler.current_option = 0
        await handler.handle_cycle_option("test_serial")
        assert handler.current_option == 1

    @pytest.mark.asyncio
    async def test_cycle_option_wraps(self, handler):
        """handle_cycle_option should wrap around to 0."""
        handler.current_option = len(handler.option_names) - 1
        await handler.handle_cycle_option("test_serial")
        assert handler.current_option == 0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[3])
    async def test_cycle_option_full_loop(self, handler):
        """A full MOVE-cycle should visit each option in order and wrap.

        sensitivity (0) -> num_teams (1) -> force_all_start (2) ->
        agent_control (3) -> sensitivity (0). Guards against off-by-one / modulo
        bugs with the current list length.
        """
        handler.current_option = 0
        expected_sequence = ["num_teams", "force_all_start", "agent_control", "sensitivity"]
        for expected_name in expected_sequence:
            await handler.handle_cycle_option("test_serial")
            assert handler.option_names[handler.current_option] == expected_name

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[3])
    async def test_cycle_option_shows_each_option_colour(self, handler, mock_state_manager):
        """Move shows the selected option's colour: sensitivity blue, num_teams turquoise,
        force_all_start orange, agent_control purple."""
        expected = [Colors.Blue.value, Colors.Turquoise.value, Colors.Orange.value, Colors.Purple.value]
        handler.current_option = len(handler.option_names) - 1  # the next Move selects index 0
        sleep = create_autospec(asyncio.sleep)  # the restore-to-white task must not run here
        with patch("services.menu.handlers.admin.asyncio.sleep", sleep):
            for colour in expected:
                mock_state_manager.led.send_base_color.reset_mock()
                await handler.handle_cycle_option("test_serial")
                assert mock_state_manager.led.send_base_color.await_args_list[0] == call("test_serial", colour)
            for task in list(handler._pending_tasks):
                task.cancel()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[3], br=["BR-005"])
    async def test_cycle_option_restores_white_after_the_option_colour(self, handler, mock_state_manager):
        """BR-005 Move: the option colour shows for about 1 s, then the LED is white again (patched clock)."""
        sleep = create_autospec(asyncio.sleep)
        handler.current_option = 0
        with patch("services.menu.handlers.admin.asyncio.sleep", sleep):
            await handler.handle_cycle_option("test_serial")
            await asyncio.gather(*handler._pending_tasks)

        sleep.assert_awaited_once_with(1.1)
        assert mock_state_manager.led.send_base_color.await_args_list == [
            call("test_serial", Colors.Turquoise.value),
            call("test_serial", Colors.White.value),
        ]

    @pytest.mark.asyncio
    async def test_cycle_option_index_always_in_bounds(self, handler):
        """current_option must stay a valid index across many cycles."""
        handler.current_option = 0
        for _ in range(10):
            await handler.handle_cycle_option("test_serial")
            assert 0 <= handler.current_option < len(handler.option_names)


class TestIncreaseValueSensitivity:
    """Tests for sensitivity increase via FlagServiceWriter."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[4, 5])
    async def test_sensitivity_cycles_forward(self, handler, mock_state_manager):
        """Sensitivity increase should call cycle_variant with forward=True."""
        handler.current_option = 0  # sensitivity

        await handler.handle_increase_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("sensitivity", forward=True)

    @pytest.mark.asyncio
    async def test_sensitivity_wraps_via_writer(self, handler, mock_state_manager):
        """Sensitivity wrapping is handled by FlagServiceWriter.cycle_variant."""
        handler.current_option = 0
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "ultra_slow"

        await handler.handle_increase_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("sensitivity", forward=True)


class TestIncreaseValueNumTeams:
    """Tests for num_teams increase via FlagServiceWriter."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[4, 5])
    async def test_num_teams_cycles_forward(self, handler, mock_state_manager):
        """num_teams increase should call cycle_variant with forward=True."""
        handler.current_option = 1  # num_teams

        await handler.handle_increase_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("num_teams", forward=True)


class TestIncreaseValueBoolean:
    """Tests for boolean settings increase (toggle) via FlagServiceWriter."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[4, 5])
    async def test_force_all_start_cycles_forward(self, handler, mock_state_manager):
        """force_all_start toggle should call cycle_variant with forward=True."""
        handler.current_option = 2  # force_all_start (index 2 in reduced surface)

        await handler.handle_increase_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("force_all_start", forward=True)


class TestWriteFailure:
    """UC-012 A5: a flag write that fails persists nothing, gives no success feedback and the error cue."""

    @staticmethod
    def _assert_error_cue_only(manager, not_saved_voice=True):
        from proto import controller_manager_pb2

        manager.led.send_game_effect.assert_called_once()
        call = manager.led.send_game_effect.call_args
        assert call[0][1] == controller_manager_pb2.GAME_EFFECT_PULSE
        assert call.kwargs["color"] == Colors.Red.value
        assert call.kwargs["duration_ms"] == 400
        assert call.kwargs["speed"] == 10
        if not_saved_voice:  # a failed write says so (#1746); a failed read stays LED-only
            manager.audio.play_voice.assert_awaited_once_with(Sound.MENU_VOX_ADMIN_NOT_SAVED)
        else:
            manager.audio.play_voice.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A5")
    @pytest.mark.parametrize("handler_method", ["handle_increase_value", "handle_decrease_value", "handle_sensitivity"])
    async def test_failed_cycle_gives_only_the_error_cue_and_stays_in_admin(
        self, handler, mock_state_manager, handler_method
    ):
        """cycle_variant returns None (write failed): a short red pulse, no voice, the host stays in admin."""
        handler.current_option = 0  # sensitivity
        mock_state_manager.game_settings_writer.cycle_variant.return_value = None

        await getattr(handler, handler_method)("test_serial")

        self._assert_error_cue_only(mock_state_manager)
        assert handler.active is True
        assert handler.controller_serial == "test_serial"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A5")
    async def test_failed_instructions_toggle_gives_the_error_cue(self, handler, mock_state_manager):
        mock_state_manager.user_prefs_writer.cycle_variant.return_value = None

        await handler.handle_instructions("test_serial")

        self._assert_error_cue_only(mock_state_manager)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A5")
    @pytest.mark.parametrize("handler_method", ["handle_increase_value", "handle_decrease_value", "handle_sensitivity"])
    async def test_raising_write_gives_the_error_cue(self, handler, mock_state_manager, handler_method):
        """cycle_variant raises (not just None): the same error cue, the host stays in admin."""
        handler.current_option = 0
        mock_state_manager.game_settings_writer.cycle_variant.side_effect = RuntimeError("boom")

        await getattr(handler, handler_method)("test_serial")

        self._assert_error_cue_only(mock_state_manager)
        assert handler.active is True

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A5")
    async def test_raising_instructions_write_gives_the_error_cue(self, handler, mock_state_manager):
        mock_state_manager.user_prefs_writer.cycle_variant.side_effect = RuntimeError("boom")

        await handler.handle_instructions("test_serial")

        self._assert_error_cue_only(mock_state_manager)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A5")
    async def test_feedback_error_after_a_good_write_gives_no_error_cue(self, handler, mock_state_manager):
        """A voice error after a write that applied is not a write failure: no red cue."""
        handler.current_option = 0
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "fast"
        handler._play_value_voice = AsyncMock(side_effect=RuntimeError("voice down"))

        await handler.handle_increase_value("test_serial")

        for effect_call in mock_state_manager.led.send_game_effect.call_args_list:
            assert effect_call.kwargs.get("duration_ms") != 400

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A5")
    async def test_successful_step_gives_no_error_cue(self, handler, mock_state_manager):
        """A write that applied plays the success feedback, never the red 400 ms error pulse."""
        handler.current_option = 0
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "fast"

        await handler.handle_increase_value("test_serial")

        for effect_call in mock_state_manager.led.send_game_effect.call_args_list:
            assert effect_call.kwargs.get("duration_ms") != 400
        mock_state_manager.audio.play_voice.assert_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A7")
    async def test_unreadable_flag_service_on_move_gives_the_error_cue(self, handler, mock_state_manager):
        """Move with the flag service unreadable: the option colour and a short red pulse, no value voice."""
        handler.current_option = len(handler.option_names) - 1  # next Move press wraps to sensitivity
        mock_state_manager.game_settings_writer.get_flag_view.return_value = None

        await handler.handle_cycle_option("test_serial")

        self._assert_error_cue_only(mock_state_manager, not_saved_voice=False)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A7")
    async def test_readable_flag_service_on_move_gives_no_error_cue(self, handler, mock_state_manager):
        handler.current_option = len(handler.option_names) - 1
        mock_state_manager.game_settings_writer.get_flag_view.return_value = flag_view(SENSITIVITY_VALUES, "fast")

        await handler.handle_cycle_option("test_serial")

        for effect_call in mock_state_manager.led.send_game_effect.call_args_list:
            assert effect_call.kwargs.get("duration_ms") != 400


class TestDecreaseValueSensitivity:
    """Tests for sensitivity decrease via FlagServiceWriter."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[4, 5])
    async def test_sensitivity_cycles_backward(self, handler, mock_state_manager):
        """Sensitivity decrease should call cycle_variant with forward=False."""
        handler.current_option = 0  # sensitivity

        await handler.handle_decrease_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("sensitivity", forward=False)


class TestDecreaseValueNumTeams:
    """Tests for num_teams decrease via FlagServiceWriter."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[4, 5])
    async def test_num_teams_cycles_backward(self, handler, mock_state_manager):
        """num_teams decrease should call cycle_variant with forward=False."""
        handler.current_option = 1  # num_teams

        await handler.handle_decrease_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("num_teams", forward=False)


class TestDecreaseValueForceAllStart:
    """Tests for force_all_start decrease via FlagServiceWriter."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[4, 5])
    async def test_force_all_start_cycles_backward(self, handler, mock_state_manager):
        """force_all_start decrease should call cycle_variant with forward=False."""
        handler.current_option = 2  # force_all_start (index 2 in reduced surface)

        await handler.handle_decrease_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("force_all_start", forward=False)


class TestShowValueFeedback:
    """Tests for visual feedback."""

    @pytest.mark.asyncio
    async def test_feedback_sends_game_effect(self, handler, mock_state_manager):
        """_show_value_feedback should send GAME_EFFECT_PULSE."""
        from proto import controller_manager_pb2

        await handler._show_value_feedback("test_serial", "sensitivity", 2)

        mock_state_manager.led.send_game_effect.assert_called_once()
        call_args = mock_state_manager.led.send_game_effect.call_args
        assert call_args[0][1] == controller_manager_pb2.GAME_EFFECT_PULSE

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[5])
    async def test_sensitivity_feedback_uses_correct_color(self, handler, mock_state_manager):
        """Sensitivity feedback should use the correct color for each level."""
        expected_colors = [
            Colors.Blue.value,
            Colors.Turquoise.value,
            Colors.Green.value,
            Colors.Orange.value,
            Colors.Red.value,
        ]

        for level, expected_color in enumerate(expected_colors):
            mock_state_manager.led.send_game_effect.reset_mock()
            await handler._show_value_feedback("test_serial", "sensitivity", level)

            call_kwargs = mock_state_manager.led.send_game_effect.call_args[1]
            assert call_kwargs["color"] == expected_color

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[5])
    async def test_boolean_feedback_green_for_true(self, handler, mock_state_manager):
        """Boolean True should show green feedback."""
        await handler._show_value_feedback("test_serial", "force_all_start", True)

        call_kwargs = mock_state_manager.led.send_game_effect.call_args[1]
        assert call_kwargs["color"] == Colors.Green.value

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[5])
    async def test_boolean_feedback_red_for_false(self, handler, mock_state_manager):
        """Boolean False should show red feedback."""
        await handler._show_value_feedback("test_serial", "force_all_start", False)

        call_kwargs = mock_state_manager.led.send_game_effect.call_args[1]
        assert call_kwargs["color"] == Colors.Red.value


class TestStateManagerIntegration:
    """Tests for state_manager.game_settings_writer integration."""

    @pytest.mark.asyncio
    async def test_uses_flag_service_writer_not_settings_service(self, handler, mock_state_manager):
        """Handler should use FlagServiceWriter, not Settings gRPC service."""
        handler.current_option = 0  # sensitivity

        # This should NOT make any gRPC calls - only calls FlagServiceWriter
        await handler.handle_increase_value("test_serial")

        # Verify cycle_variant was called on the writer
        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("sensitivity", forward=True)

    @pytest.mark.asyncio
    async def test_no_state_manager_returns_early(self, handler):
        """Handler should return early if state_manager is None."""
        handler._state_manager = None

        # Should not raise
        await handler.handle_increase_value("test_serial")
        await handler.handle_decrease_value("test_serial")


class TestHandleSensitivityLocalStorage:
    """Tests for handle_sensitivity using FlagServiceWriter."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A1")
    async def test_handle_sensitivity_uses_flag_service_writer(self, handler, mock_state_manager):
        """handle_sensitivity should use FlagServiceWriter.cycle_variant."""
        mock_state_manager.game_settings_writer.get_current_variant.return_value = "medium"
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "fast"

        await handler.handle_sensitivity("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("sensitivity", forward=True)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A1")
    @pytest.mark.parametrize(
        ("variant", "colour", "voice"),
        [
            ("ultra_slow", Colors.Blue, Sound.MENU_VOX_SENSITIVITY_ULTRA_HIGH),
            ("slow", Colors.Turquoise, Sound.MENU_VOX_SENSITIVITY_HIGH),
            ("medium", Colors.Green, Sound.MENU_VOX_SENSITIVITY_MEDIUM),
            ("fast", Colors.Orange, Sound.MENU_VOX_SENSITIVITY_LOW),
            ("ultra_fast", Colors.Red, Sound.MENU_VOX_SENSITIVITY_ULTRA_LOW),
        ],
    )
    async def test_handle_sensitivity_pulses_level_colour_and_plays_level_voice(
        self, handler, mock_state_manager, variant, colour, voice
    ):
        """Circle: the new level is shown as a colour pulse and announced with its voice line."""
        from proto import controller_manager_pb2

        mock_state_manager.game_settings_writer.cycle_variant.return_value = variant
        mock_state_manager.game_settings_writer.get_flag_view.return_value = flag_view(SENSITIVITY_VALUES, variant)

        await handler.handle_sensitivity("test_serial")

        effect_call = mock_state_manager.led.send_game_effect.await_args
        assert effect_call.args == ("test_serial", controller_manager_pb2.GAME_EFFECT_PULSE)
        assert effect_call.kwargs["color"] == colour.value
        mock_state_manager.audio.play_voice.assert_awaited_once_with(voice)

    @pytest.mark.asyncio
    async def test_handle_sensitivity_wrapping_handled_by_writer(self, handler, mock_state_manager):
        """Sensitivity wrapping is delegated to FlagServiceWriter.cycle_variant."""
        mock_state_manager.game_settings_writer.get_current_variant.return_value = "ultra_fast"
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "ultra_slow"

        await handler.handle_sensitivity("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("sensitivity", forward=True)


class TestHandleForceStartFlagd:
    """Tests for handle_force_start using flagd."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="main", steps=[3])
    @pytest.mark.uc("UC-012", br=["BR-001"])
    async def test_handle_force_start_reads_from_flagd(self, handler, mock_state_manager):
        """handle_force_start should read force_all_start from flagd."""
        mock_state_manager.connected_controllers = {"s1", "s2"}
        mock_state_manager.ready_controllers = {"s1"}
        handler._publish_event = AsyncMock()
        handler.exit = AsyncMock()

        # Mock the flagd client used inside handle_force_start
        mock_gs_client = MagicMock()
        mock_gs_client.get_boolean_value = MagicMock(return_value=True)

        with (
            patch("services.menu.handlers.admin.asyncio.sleep", new_callable=AsyncMock),
            patch("lib.feature_flags.get_flag_client", return_value=mock_gs_client),
        ):
            await handler.handle_force_start("test_serial")

        # Should have read force_all_start from flagd
        mock_gs_client.get_boolean_value.assert_called_once_with("force_all_start", False)
        # Should have used all connected controllers since force_all_start=True
        handler._publish_event.assert_called()
        call_args = handler._publish_event.call_args[0]
        assert call_args[0] == "game_requested"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="main", steps=[5])
    async def test_handle_force_start_invokes_coordinator_start(self, handler, mock_state_manager):
        """Force start must actually launch the game through the coordinator start
        path (#1029) — not merely publish the menu event. The selected mode +
        force_all_start-aware roster must be passed to the start callback."""
        mock_state_manager.current_game_mode.name = "JoustTeams"
        mock_state_manager.connected_controllers = {"s1", "s2", "s3"}
        mock_state_manager.ready_controllers = {"s1"}
        handler._publish_event = AsyncMock()
        handler.exit = AsyncMock()

        # force_all_start=True -> start with ALL connected controllers.
        mock_gs_client = MagicMock()
        mock_gs_client.get_boolean_value = MagicMock(return_value=True)

        with (
            patch("services.menu.handlers.admin.asyncio.sleep", new_callable=AsyncMock),
            patch("lib.feature_flags.get_flag_client", return_value=mock_gs_client),
        ):
            await handler.handle_force_start("test_serial")

        handler.callbacks.start_game.assert_awaited_once()
        args = handler.callbacks.start_game.await_args
        assert args.args[0] == "test_serial"
        assert args.args[1] == "admin_force_start"
        # Roster honors force_all_start: all connected controllers.
        roster = args.args[2]
        assert set(roster) == {"s1", "s2", "s3"}

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="main", steps=[3])
    async def test_handle_force_start_roster_uses_ready_when_not_force_all(self, handler, mock_state_manager):
        """Without force_all_start, the start roster is the ready controllers
        plus the admin controller (#1029)."""
        mock_state_manager.current_game_mode.name = "JoustFFA"
        mock_state_manager.connected_controllers = {"s1", "s2", "s3"}
        mock_state_manager.ready_controllers = {"s1", "s2"}
        handler._publish_event = AsyncMock()
        handler.exit = AsyncMock()

        mock_gs_client = MagicMock()
        mock_gs_client.get_boolean_value = MagicMock(return_value=False)

        with (
            patch("services.menu.handlers.admin.asyncio.sleep", new_callable=AsyncMock),
            patch("lib.feature_flags.get_flag_client", return_value=mock_gs_client),
        ):
            await handler.handle_force_start("admin_serial")

        handler.callbacks.start_game.assert_awaited_once()
        roster = handler.callbacks.start_game.await_args.args[2]
        # ready (s1, s2) + admin (admin_serial), no duplicates.
        assert set(roster) == {"s1", "s2", "admin_serial"}

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="main", steps=[3])
    async def test_force_start_not_blocked_by_ghost_controller(self, handler, mock_state_manager):
        """Regression for #551: a ghost controller (the menu tracks more
        connected controllers than are ready, e.g. after a missed DISCONNECT
        during a gRPC GOAWAY) must NOT block admin force-start.

        The normal trigger/auto path gates on all_ready() (ready == connected),
        which stays False while a ghost lingers, so the game can never start
        that way. Admin force-start is the operator's escape hatch: it must
        bypass that gate entirely and launch with the real ready set rather
        than waiting for the phantom controller to become ready.
        """
        mock_state_manager.current_game_mode.name = "JoustFFA"
        # 16 ready, but the menu still tracks a 17th "ghost" as connected after a
        # missed disconnect -> connected_count (17) != ready_count (16). The
        # all-ready gate would refuse to start here.
        ready = {f"s{i}" for i in range(16)}
        mock_state_manager.ready_controllers = set(ready)
        mock_state_manager.connected_controllers = ready | {"ghost"}
        # The all-ready gate is False while the ghost lingers (ready != connected).
        # Pin it explicitly so this test fails the instant force-start regresses to
        # honoring that gate (a default MagicMock is truthy and would mask it).
        mock_state_manager.all_ready = MagicMock(return_value=False)
        handler._publish_event = AsyncMock()
        handler.exit = AsyncMock()

        # force_all_start=False -> roster is the real ready set (+ admin if not
        # already ready). The ghost is NOT ready, so it is excluded.
        mock_gs_client = MagicMock()
        mock_gs_client.get_boolean_value = MagicMock(return_value=False)

        with (
            patch("services.menu.handlers.admin.asyncio.sleep", new_callable=AsyncMock),
            patch("lib.feature_flags.get_flag_client", return_value=mock_gs_client),
        ):
            await handler.handle_force_start("s0")

        # The game must actually start despite the ready/connected mismatch.
        handler.callbacks.start_game.assert_awaited_once()
        roster = handler.callbacks.start_game.await_args.args[2]
        # Started with the real ready set; the ghost is ignored. Admin (s0) is
        # already ready, so no extra controller is appended.
        assert set(roster) == ready
        assert "ghost" not in roster


def _patch_live_roster(serials):
    """Patch the controller-manager stub so GetConnectedControllers returns
    a response whose connected_serials is ``serials``."""
    response = MagicMock()
    response.connected_serials = list(serials)
    stub = MagicMock()
    stub.GetConnectedControllers = AsyncMock(return_value=response)
    return patch(
        "proto.controller_manager_pb2_grpc.ControllerManagerServiceStub",
        return_value=stub,
    )


class TestForceStartLiveRosterReconcile:
    """#1153/#551: force-start prunes ghosts against the CM live-roster RPC."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="main", steps=[4], br=["BR-001"])
    async def test_force_all_prunes_ghost_via_live_roster(self, handler, mock_state_manager):
        """force_all_start=True uses the full connected set, which still contains
        a ghost after a missed DISCONNECT. The live-roster RPC returns the real
        backend set (no ghost) so force-start launches without it (#1153)."""
        mock_state_manager.current_game_mode.name = "JoustFFA"
        real = {f"s{i}" for i in range(16)}
        mock_state_manager.connected_controllers = real | {"ghost"}
        mock_state_manager.ready_controllers = set(real)
        handler._publish_event = AsyncMock()
        handler.exit = AsyncMock()

        mock_gs_client = MagicMock()
        mock_gs_client.get_boolean_value = MagicMock(return_value=True)  # force_all_start

        with (
            patch("services.menu.handlers.admin.asyncio.sleep", new_callable=AsyncMock),
            patch("lib.feature_flags.get_flag_client", return_value=mock_gs_client),
            _patch_live_roster(real),  # backend's authoritative roster: no ghost
        ):
            await handler.handle_force_start("s0")

        handler.callbacks.start_game.assert_awaited_once()
        roster = handler.callbacks.start_game.await_args.args[2]
        assert "ghost" not in roster
        assert set(roster) == real
        # One ghost pruned -> the metric was incremented.
        handler.metrics.ghost_controllers_pruned_total.inc.assert_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="A2")
    async def test_force_start_fail_soft_on_rpc_error(self, handler, mock_state_manager):
        """If the live-roster RPC errors, force-start keeps the menu roster and
        still launches (must never be blocked by a reconcile failure)."""
        mock_state_manager.current_game_mode.name = "JoustFFA"
        real = {f"s{i}" for i in range(16)}
        mock_state_manager.connected_controllers = real | {"ghost"}
        mock_state_manager.ready_controllers = set(real)
        handler._publish_event = AsyncMock()
        handler.exit = AsyncMock()

        mock_gs_client = MagicMock()
        mock_gs_client.get_boolean_value = MagicMock(return_value=True)

        failing_stub = MagicMock()
        failing_stub.GetConnectedControllers = AsyncMock(side_effect=RuntimeError("RPC down"))

        with (
            patch("services.menu.handlers.admin.asyncio.sleep", new_callable=AsyncMock),
            patch("lib.feature_flags.get_flag_client", return_value=mock_gs_client),
            patch(
                "proto.controller_manager_pb2_grpc.ControllerManagerServiceStub",
                return_value=failing_stub,
            ),
        ):
            await handler.handle_force_start("s0")

        # Fail-soft: the game still starts with the menu-derived roster (ghost incl.).
        handler.callbacks.start_game.assert_awaited_once()
        roster = handler.callbacks.start_game.await_args.args[2]
        assert set(roster) == real | {"ghost"}
        # The degraded-reconcile counter is incremented so the dashboard sees it.
        handler.metrics.force_start_reconcile_failed_total.inc.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="main", steps=[4], br=["BR-001"])
    async def test_admin_serial_preserved_when_absent_from_roster(self, handler, mock_state_manager):
        """The admin controller that triggered the start is never pruned, even if
        the live roster omits it (avoids starting with an empty roster)."""
        mock_state_manager.current_game_mode.name = "JoustFFA"
        mock_state_manager.connected_controllers = {"admin", "ghost"}
        mock_state_manager.ready_controllers = {"admin", "ghost"}
        handler._publish_event = AsyncMock()
        handler.exit = AsyncMock()

        mock_gs_client = MagicMock()
        mock_gs_client.get_boolean_value = MagicMock(return_value=True)

        with (
            patch("services.menu.handlers.admin.asyncio.sleep", new_callable=AsyncMock),
            patch("lib.feature_flags.get_flag_client", return_value=mock_gs_client),
            _patch_live_roster(set()),  # backend reports nothing connected
        ):
            await handler.handle_force_start("admin")

        roster = handler.callbacks.start_game.await_args.args[2]
        assert "admin" in roster
        assert "ghost" not in roster


# ============================================================================
# Tier 1 — Core Lifecycle
# ============================================================================


class TestEnter:
    """Tests for entering admin mode."""

    @pytest.mark.asyncio
    async def test_enter_sets_active(self, inactive_handler):
        """enter() should set active=True."""
        await inactive_handler.enter("s1")
        assert inactive_handler.active is True

    @pytest.mark.asyncio
    async def test_enter_sets_serial(self, inactive_handler):
        """enter() should record the admin controller serial."""
        await inactive_handler.enter("s1")
        assert inactive_handler.controller_serial == "s1"

    @pytest.mark.asyncio
    async def test_enter_sets_entry_time(self, inactive_handler):
        """enter() should record the entry time."""
        before = time.time()
        await inactive_handler.enter("s1")
        assert inactive_handler.entry_time >= before

    @pytest.mark.asyncio
    async def test_enter_resets_option_to_zero(self, inactive_handler):
        """enter() should reset current_option to 0."""
        inactive_handler.current_option = 5
        await inactive_handler.enter("s1")
        assert inactive_handler.current_option == 0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[2])
    async def test_enter_sends_admin_enter_effect(self, inactive_handler, mock_state_manager):
        """enter() should send GAME_EFFECT_ADMIN_ENTER."""
        from proto import controller_manager_pb2

        await inactive_handler.enter("s1")
        call_args = mock_state_manager.led.send_game_effect.call_args
        assert call_args[0][0] == "s1"
        assert call_args[0][1] == controller_manager_pb2.GAME_EFFECT_ADMIN_ENTER

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[2])
    async def test_enter_starts_timeout_task(self, inactive_handler):
        """enter() should create a timeout task."""
        await inactive_handler.enter("s1")
        assert inactive_handler._timeout_task is not None
        assert not inactive_handler._timeout_task.done()
        # Clean up
        inactive_handler._timeout_task.cancel()

    @pytest.mark.asyncio
    async def test_enter_creates_session_span(self, inactive_handler, mock_tracer):
        """enter() should create a session span."""
        await inactive_handler.enter("s1")
        mock_tracer.start_span.assert_called_with("admin_mode_session")
        assert inactive_handler.session_span is not None
        # Clean up
        if inactive_handler._timeout_task:
            inactive_handler._timeout_task.cancel()


class TestExit:
    """Tests for exiting admin mode."""

    @pytest.mark.asyncio
    async def test_exit_returns_early_if_not_active(self, inactive_handler, mock_state_manager):
        """exit() should return immediately if not active."""
        await inactive_handler.exit()
        mock_state_manager.led.send_game_effect.assert_not_called()

    @pytest.mark.asyncio
    async def test_exit_clears_active(self, handler):
        """exit() should set active=False."""
        await handler.exit()
        assert handler.active is False

    @pytest.mark.asyncio
    async def test_exit_clears_serial(self, handler):
        """exit() should clear controller_serial."""
        await handler.exit()
        assert handler.controller_serial is None

    @pytest.mark.asyncio
    async def test_exit_clears_entry_time(self, handler):
        """exit() should reset entry_time to 0."""
        await handler.exit()
        assert handler.entry_time == 0

    @pytest.mark.asyncio
    async def test_exit_sends_admin_exit_effect(self, handler, mock_state_manager):
        """exit() should send GAME_EFFECT_ADMIN_EXIT."""
        from proto import controller_manager_pb2

        await handler.exit()
        call_args = mock_state_manager.led.send_game_effect.call_args
        assert call_args[0][0] == "test_serial"
        assert call_args[0][1] == controller_manager_pb2.GAME_EFFECT_ADMIN_EXIT

    @pytest.mark.asyncio
    async def test_exit_cancels_timeout_task(self, handler):
        """exit() should cancel the timeout task."""
        # Create a fake timeout task
        mock_task = MagicMock()
        mock_task.done.return_value = False
        handler._timeout_task = mock_task

        await handler.exit()

        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_exit_ends_session_span(self, handler):
        """exit() should end the session span."""
        span = MagicMock()
        handler.session_span = span

        await handler.exit()

        span.end.assert_called_once()
        assert handler.session_span is None
        assert handler.session_span_context is None


class TestHandleButtonEventDispatch:
    """Tests for button event routing in handle_button_event."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[3], br=["BR-005"])
    async def test_dispatch_move_to_cycle_option(self, handler):
        """MOVE button should dispatch to handle_cycle_option."""
        handler.handle_cycle_option = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.MOVE)
        handler.handle_cycle_option.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A6", br=["BR-005"])
    async def test_dispatch_trigger_to_hold_tracking(self, handler):
        """TRIGGER button should dispatch to _start_trigger_hold_tracking."""
        handler._start_trigger_hold_tracking = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.TRIGGER)
        handler._start_trigger_hold_tracking.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[4], br=["BR-005"])
    async def test_dispatch_select_to_increase(self, handler):
        """SELECT button should dispatch to handle_increase_value."""
        handler.handle_increase_value = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.SELECT)
        handler.handle_increase_value.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[4], br=["BR-005"])
    async def test_dispatch_start_to_decrease(self, handler):
        """START button should dispatch to handle_decrease_value."""
        handler.handle_decrease_value = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.START)
        handler.handle_decrease_value.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="main", steps=[1])
    @pytest.mark.uc("UC-012", br=["BR-005"])
    async def test_dispatch_cross_to_game_mode(self, handler):
        """CROSS button should dispatch to handle_game_mode."""
        handler.handle_game_mode = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.CROSS)
        handler.handle_game_mode.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A1", br=["BR-005"])
    async def test_dispatch_circle_to_sensitivity(self, handler):
        """CIRCLE button should dispatch to handle_sensitivity."""
        handler.handle_sensitivity = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.CIRCLE)
        handler.handle_sensitivity.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A2", br=["BR-005"])
    async def test_dispatch_triangle_to_battery(self, handler):
        """TRIANGLE button should dispatch to handle_battery."""
        handler.handle_battery = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.TRIANGLE)
        handler.handle_battery.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A3", br=["BR-005"])
    async def test_dispatch_square_to_instructions(self, handler):
        """SQUARE button should dispatch to handle_instructions."""
        handler.handle_instructions = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.SQUARE)
        handler.handle_instructions.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[7], br=["BR-005"])
    async def test_dispatch_ps_to_exit(self, handler):
        """PS button should dispatch to _exit_to_connected."""
        handler._exit_to_connected = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.PS)
        handler._exit_to_connected.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A4")
    @pytest.mark.nfr("NFR-006")
    async def test_timeout_exits_after_60_seconds(self, handler):
        """Should auto-exit if entry_time was more than 60 seconds ago."""
        assert handler._timeout_seconds == 60
        handler.entry_time = time.time() - 61
        handler._exit_to_connected = AsyncMock()

        await handler.handle_button_event("test_serial", ButtonTrackingKey.CROSS)

        handler._exit_to_connected.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A4")
    async def test_activity_does_not_extend_the_timeout(self, inactive_handler, mock_state_manager):
        """A press during the session neither restarts the timer nor moves the entry time; the timer
        started at entry exits admin mode."""
        clock = [1000.0]
        fake_time = create_autospec(time, instance=False)
        fake_time.time.side_effect = lambda: clock[0]
        mock_state_manager.transition_to = AsyncMock()
        inactive_handler.handle_game_mode = AsyncMock()
        inactive_handler._timeout_seconds = 0.05

        with patch("services.menu.handlers.admin.time", fake_time):
            await inactive_handler.enter("A")
            timer = inactive_handler._timeout_task
            clock[0] += 0.02  # partway through the session
            await inactive_handler.handle_button_event("A", ButtonTrackingKey.CROSS)

            inactive_handler.handle_game_mode.assert_awaited_once_with("A")
            assert inactive_handler.entry_time == 1000.0
            assert inactive_handler._timeout_task is timer
            await timer

        mock_state_manager.transition_to.assert_awaited_once_with("A", ControllerState.CONNECTED)
        assert inactive_handler.active is False

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A4")
    @pytest.mark.nfr("NFR-006")
    async def test_press_check_uses_the_timer_limit(self, handler):
        """The per-press check and the timer task share _timeout_seconds (one NFR-006 source)."""
        handler._timeout_seconds = 5
        handler.handle_game_mode = AsyncMock()
        handler._exit_to_connected = AsyncMock()

        handler.entry_time = time.time() - 4
        await handler.handle_button_event("test_serial", ButtonTrackingKey.CROSS)
        handler._exit_to_connected.assert_not_called()
        handler.handle_game_mode.assert_awaited_once_with("test_serial")

        handler.entry_time = time.time() - 6
        await handler.handle_button_event("test_serial", ButtonTrackingKey.SQUARE)
        handler._exit_to_connected.assert_awaited_once_with("test_serial")

    @pytest.mark.asyncio
    async def test_debounce_rejects_rapid_presses(self, handler):
        """Rapid presses should be debounced."""
        handler.handle_game_mode = AsyncMock()

        # First press should go through
        await handler.handle_button_event("test_serial", ButtonTrackingKey.CROSS)
        assert handler.handle_game_mode.call_count == 1

        # Immediate second press should be debounced
        await handler.handle_button_event("test_serial", ButtonTrackingKey.CROSS)
        assert handler.handle_game_mode.call_count == 1

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", br=["BR-007"])
    async def test_debounce_uses_event_time_not_processing_time(self, handler):
        """Presses 400 ms apart by event time both apply when processed back to back (stalled loop)."""
        handler.handle_game_mode = AsyncMock()
        t0 = 1_700_000_000.0

        await handler.handle_button_event("test_serial", ButtonTrackingKey.CROSS, t0)
        await handler.handle_button_event("test_serial", ButtonTrackingKey.CROSS, t0 + 0.4)
        assert handler.handle_game_mode.call_count == 2

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", br=["BR-007"])
    async def test_debounce_event_time_still_rejects_bounce(self, handler):
        """Presses 100 ms apart by event time are one press, even if processed far apart."""
        handler.handle_game_mode = AsyncMock()
        t0 = 1_700_000_000.0

        await handler.handle_button_event("test_serial", ButtonTrackingKey.CROSS, t0)
        await handler.handle_button_event("test_serial", ButtonTrackingKey.CROSS, t0 + 0.1)
        assert handler.handle_game_mode.call_count == 1


class TestVariantToValue:
    """Tests for _variant_to_value static method."""

    def test_sensitivity_ultra_slow(self):
        assert AdminModeHandler._variant_to_value("sensitivity", "ultra_slow") == 0

    def test_sensitivity_ultra_fast(self):
        assert AdminModeHandler._variant_to_value("sensitivity", "ultra_fast") == 4

    def test_num_teams_two(self):
        assert AdminModeHandler._variant_to_value("num_teams", "two") == 2

    def test_num_teams_six(self):
        assert AdminModeHandler._variant_to_value("num_teams", "six") == 6

    def test_force_all_start_on(self):
        assert AdminModeHandler._variant_to_value("force_all_start", "on") is True

    def test_force_all_start_off(self):
        assert AdminModeHandler._variant_to_value("force_all_start", "off") is False

    def test_unknown_option_returns_zero(self):
        assert AdminModeHandler._variant_to_value("unknown_option", "whatever") == 0

    def test_unknown_variant_returns_zero(self):
        assert AdminModeHandler._variant_to_value("sensitivity", "nonexistent") == 0


class TestCheckCombo:
    """Tests for admin combo detection."""

    def test_all_four_buttons_true(self, handler):
        """All 4 face buttons pressed should return True."""
        state = {"cross": True, "circle": True, "square": True, "triangle": True}
        assert handler.check_combo_from_state(state) is True

    def test_missing_one_button(self, handler):
        """Missing any button should return False."""
        state = {"cross": True, "circle": True, "square": True, "triangle": False}
        assert handler.check_combo_from_state(state) is False

    def test_empty_state(self, handler):
        """Empty dict should return False."""
        assert handler.check_combo_from_state({}) is False


class TestIsAdminController:
    """Tests for is_admin_controller."""

    def test_matching_active_returns_true(self, handler):
        """Matching serial when active should return True."""
        assert handler.is_admin_controller("test_serial") is True

    def test_wrong_serial_returns_false(self, handler):
        """Wrong serial should return False."""
        assert handler.is_admin_controller("other_serial") is False

    def test_inactive_returns_false(self, inactive_handler):
        """Inactive handler should return False for any serial."""
        assert inactive_handler.is_admin_controller("s1") is False


class TestStateProperty:
    """Tests for the state property."""

    def test_state_is_admin(self, handler):
        """state property should return ControllerState.ADMIN."""
        assert handler.state == ControllerState.ADMIN


# ============================================================================
# Tier 3 — Button Handlers
# ============================================================================


class TestHandleGameMode:
    """Tests for game mode cycling (cross button)."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="main", steps=[1, 2])
    async def test_cycles_to_next_mode(self, handler, mock_state_manager):
        """Should cycle to next game mode."""
        mock_state_manager.current_game_mode.name = "JoustFFA"
        handler.callbacks.get_game_options.return_value = ["JoustFFA", "JoustTeams", "Werewolf"]

        await handler.handle_game_mode("test_serial")

        mock_state_manager.publish_event.assert_called_once()
        call_args = mock_state_manager.publish_event.call_args[0]
        assert call_args[0] == "game_selection_changed"
        assert call_args[1]["game_name"] == "JoustTeams"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="main", steps=[2])
    async def test_wraps_to_first_mode(self, handler, mock_state_manager):
        """Should wrap to first mode when at end."""
        mock_state_manager.current_game_mode.name = "JoustTeams"
        handler.callbacks.get_game_options.return_value = ["JoustFFA", "JoustTeams"]

        await handler.handle_game_mode("test_serial")

        call_args = mock_state_manager.publish_event.call_args[0]
        assert call_args[1]["game_name"] == "JoustFFA"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="main", steps=[3])
    async def test_sends_pulse_and_plays_voice(self, handler, mock_state_manager):
        """Should send color pulse and play game mode voice."""
        from proto import controller_manager_pb2

        await handler.handle_game_mode("test_serial")

        from services.menu.utils.led import GAME_MODE_COLORS

        mock_state_manager.led.send_game_effect.assert_called_once()
        call_args = mock_state_manager.led.send_game_effect.call_args
        assert call_args[0][1] == controller_manager_pb2.GAME_EFFECT_PULSE
        # The pulse uses the new mode's lobby colour, the voice announces the new mode.
        assert call_args.kwargs["color"] == GAME_MODE_COLORS["JoustTeams"]
        mock_state_manager.audio.play_game_mode_voice.assert_called_once_with("JoustTeams")

    @pytest.mark.asyncio
    async def test_empty_options_returns_early(self, handler, mock_state_manager):
        """Should return early if no game options available."""
        handler.callbacks.get_game_options.return_value = []

        await handler.handle_game_mode("test_serial")

        mock_state_manager.publish_event.assert_not_called()
        handler.callbacks.select_game_mode.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="main", steps=[4])
    async def test_applies_selection_via_callback(self, handler, mock_state_manager):
        """Cross cycling must actually apply the new selection through the menu
        (#1030) — not just publish a fire-and-forget event."""
        mock_state_manager.current_game_mode.name = "JoustFFA"
        handler.callbacks.get_game_options.return_value = ["JoustFFA", "JoustTeams", "Werewolf"]

        await handler.handle_game_mode("test_serial")

        handler.callbacks.select_game_mode.assert_awaited_once_with("JoustTeams")


class TestHandleBattery:
    """Tests for battery display (triangle button)."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A2")
    async def test_sends_show_battery_with_empty_serial(self, handler, mock_state_manager):
        """Should send GAME_EFFECT_SHOW_BATTERY with empty serial (affects all)."""
        from proto import controller_manager_pb2

        await handler.handle_battery("test_serial")

        call_args = mock_state_manager.led.send_game_effect.call_args
        assert call_args[0][0] == ""
        assert call_args[0][1] == controller_manager_pb2.GAME_EFFECT_SHOW_BATTERY

    @pytest.mark.asyncio
    async def test_handles_stream_unavailable(self, handler, mock_state_manager):
        """Should handle stream not available gracefully."""
        mock_state_manager.led.send_game_effect = AsyncMock(return_value=False)

        # Should not raise
        await handler.handle_battery("test_serial")


class TestHandleInstructions:
    """Tests for instruction toggle (square button)."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A3")
    async def test_cycles_variant(self, handler, mock_state_manager):
        """Should cycle game_instructions variant."""
        await handler.handle_instructions("test_serial")

        mock_state_manager.user_prefs_writer.cycle_variant.assert_called_once_with("game_instructions", forward=True)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A3")
    async def test_enabled_shows_green_and_on_voice(self, handler, mock_state_manager):
        """When toggled to 'on', should show green and play on voice."""
        mock_state_manager.user_prefs_writer.cycle_variant.return_value = "on"

        await handler.handle_instructions("test_serial")

        call_kwargs = mock_state_manager.led.send_game_effect.call_args[1]
        assert call_kwargs["color"] == (0, 255, 0)
        mock_state_manager.audio.play_voice.assert_called_once_with(Sound.MENU_VOX_INSTRUCTIONS_ON)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A3")
    async def test_disabled_shows_red_and_off_voice(self, handler, mock_state_manager):
        """When toggled to 'off', should show red and play off voice."""
        mock_state_manager.user_prefs_writer.cycle_variant.return_value = "off"

        await handler.handle_instructions("test_serial")

        call_kwargs = mock_state_manager.led.send_game_effect.call_args[1]
        assert call_kwargs["color"] == (255, 0, 0)
        mock_state_manager.audio.play_voice.assert_called_once_with(Sound.MENU_VOX_INSTRUCTIONS_OFF)

    @pytest.mark.asyncio
    async def test_no_state_manager_returns_early(self, handler, mock_state_manager):
        """Should return early if state_manager is None."""
        handler._state_manager = None
        await handler.handle_instructions("test_serial")
        mock_state_manager.user_prefs_writer.cycle_variant.assert_not_called()


class TestTriggerHoldAndRelease:
    """Tests for trigger hold tracking and force start."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="main", steps=[1, 2])
    async def test_hold_starts_effect_and_creates_task(self, handler, mock_state_manager):
        """Trigger press should start force start effect and create hold task."""
        from proto import controller_manager_pb2

        await handler._start_trigger_hold_tracking("test_serial")

        assert handler._trigger_hold_task is not None
        call_args = mock_state_manager.led.send_game_effect.call_args
        assert call_args[0][0] == "test_serial"
        assert call_args[0][1] == controller_manager_pb2.GAME_EFFECT_FORCE_START_CHARGE
        # Clean up
        handler._trigger_hold_task.cancel()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="A1")
    async def test_release_cancels_task_and_restores_white(self, handler, mock_state_manager):
        """Trigger release should cancel hold task and restore white."""
        # Start the hold first
        await handler._start_trigger_hold_tracking("test_serial")
        mock_state_manager.led.send_game_effect.reset_mock()

        # Release
        await handler.handle_trigger_release("test_serial")

        assert handler._trigger_hold_task is None
        mock_state_manager.led.send_base_color.assert_called_with("test_serial", (255, 255, 255))

    @pytest.mark.asyncio
    async def test_release_ignored_for_non_admin(self, handler, mock_state_manager):
        """Trigger release should be ignored for non-admin controller."""
        await handler.handle_trigger_release("other_serial")
        mock_state_manager.led.send_base_color.assert_not_called()


class TestExitToConnected:
    """Tests for _exit_to_connected."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[8])
    async def test_with_state_manager_calls_transition(self, handler, mock_state_manager):
        """Should call state_manager.transition_to with CONNECTED."""
        mock_state_manager.transition_to = AsyncMock()

        await handler._exit_to_connected("test_serial")

        mock_state_manager.transition_to.assert_called_once_with("test_serial", ControllerState.CONNECTED)

    @pytest.mark.asyncio
    async def test_without_state_manager_calls_exit(self, handler):
        """Should fall back to self.exit() if no state manager."""
        handler._state_manager = None
        handler.exit = AsyncMock()

        await handler._exit_to_connected("test_serial")

        handler.exit.assert_called_once()


class TestForceStartEffect:
    """Tests for force start LED effects."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="main", steps=[2])
    async def test_start_sends_charge_effect(self, handler, mock_state_manager):
        """start_force_start_effect should send GAME_EFFECT_FORCE_START_CHARGE."""
        from proto import controller_manager_pb2

        await handler.start_force_start_effect("test_serial")

        call_args = mock_state_manager.led.send_game_effect.call_args
        assert call_args[0][0] == "test_serial"
        assert call_args[0][1] == controller_manager_pb2.GAME_EFFECT_FORCE_START_CHARGE

    @pytest.mark.asyncio
    async def test_cancel_sends_white_base_color(self, handler, mock_state_manager):
        """cancel_force_start_effect should restore white base color."""
        await handler.cancel_force_start_effect("test_serial")

        mock_state_manager.led.send_base_color.assert_called_once_with("test_serial", (255, 255, 255))


# ============================================================================
# Tier 4 — Feedback Details
# ============================================================================


class TestShowValueFeedbackDetails:
    """Detailed tests for _show_value_feedback color calculations."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[5], br=["BR-005"])
    async def test_num_teams_2_is_green(self, handler, mock_state_manager):
        """num_teams=2 should produce green (0, 255, 0)."""
        await handler._show_value_feedback("test_serial", "num_teams", 2)
        call_kwargs = mock_state_manager.led.send_game_effect.call_args[1]
        assert call_kwargs["color"] == (0, 255, 0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[5], br=["BR-005"])
    async def test_num_teams_6_is_red(self, handler, mock_state_manager):
        """num_teams=6 should produce red (255, 0, 0)."""
        await handler._show_value_feedback("test_serial", "num_teams", 6)
        call_kwargs = mock_state_manager.led.send_game_effect.call_args[1]
        assert call_kwargs["color"] == (255, 0, 0)

    @pytest.mark.asyncio
    async def test_unknown_option_shows_white(self, handler, mock_state_manager):
        """Unknown option should default to white."""
        await handler._show_value_feedback("test_serial", "unknown_option", 0)
        call_kwargs = mock_state_manager.led.send_game_effect.call_args[1]
        assert call_kwargs["color"] == Colors.White.value


class TestPlayValueVoice:
    """Tests for _play_value_voice (verifies bug fix for type mismatch)."""

    @pytest.mark.asyncio
    async def test_num_teams_voice_plays_with_int(self, handler, mock_state_manager):
        """num_teams voice should play when value is int (bug fix proof).

        Before fix: num_teams_voices.get(2) returned None because keys were strings.
        After fix: num_teams_voices.get(str(2)) returns the correct Sound.
        """
        await handler._play_value_voice("num_teams", 3)
        mock_state_manager.audio.play_voice.assert_called_once_with(Sound.MENU_VOX_ADMINOP_3)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[5])
    async def test_num_teams_voice_all_values(self, handler, mock_state_manager):
        """All num_teams values (2-6) should play correct voice."""
        expected = {
            2: Sound.MENU_VOX_ADMINOP_2,
            3: Sound.MENU_VOX_ADMINOP_3,
            4: Sound.MENU_VOX_ADMINOP_4,
            5: Sound.MENU_VOX_ADMINOP_5,
            6: Sound.MENU_VOX_ADMINOP_6,
        }
        for value, expected_sound in expected.items():
            mock_state_manager.audio.play_voice.reset_mock()
            await handler._play_value_voice("num_teams", value)
            mock_state_manager.audio.play_voice.assert_called_once_with(expected_sound)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[5])
    async def test_force_all_start_true_plays_true_voice(self, handler, mock_state_manager):
        """force_all_start=True should play TRUE voice (bug fix proof).

        Before fix: value == "true" never matched bool True.
        After fix: value is True correctly matches.
        """
        await handler._play_value_voice("force_all_start", True)
        mock_state_manager.audio.play_voice.assert_called_once_with(Sound.MENU_VOX_ADMINOP_TRUE)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[5])
    async def test_force_all_start_false_plays_false_voice(self, handler, mock_state_manager):
        """force_all_start=False should play FALSE voice."""
        await handler._play_value_voice("force_all_start", False)
        mock_state_manager.audio.play_voice.assert_called_once_with(Sound.MENU_VOX_ADMINOP_FALSE)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[5])
    async def test_sensitivity_voice_all_values(self, handler, mock_state_manager):
        """Every sensitivity level announces its voice line (UC-012 step 5)."""
        expected = {
            0: Sound.MENU_VOX_SENSITIVITY_ULTRA_HIGH,
            1: Sound.MENU_VOX_SENSITIVITY_HIGH,
            2: Sound.MENU_VOX_SENSITIVITY_MEDIUM,
            3: Sound.MENU_VOX_SENSITIVITY_LOW,
            4: Sound.MENU_VOX_SENSITIVITY_ULTRA_LOW,
        }
        for value, expected_sound in expected.items():
            mock_state_manager.audio.play_voice.reset_mock()
            await handler._play_value_voice("sensitivity", value)
            mock_state_manager.audio.play_voice.assert_called_once_with(expected_sound)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[5])
    async def test_select_on_sensitivity_plays_voice(self, handler, mock_state_manager):
        """SELECT on the sensitivity option pulses and announces the new level (UC-012 step 5)."""
        handler.current_option = handler.option_names.index("sensitivity")
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "fast"
        mock_state_manager.game_settings_writer.get_flag_view.return_value = flag_view(SENSITIVITY_VALUES, "fast")

        await handler.handle_increase_value("test_serial")

        mock_state_manager.led.send_game_effect.assert_called()
        mock_state_manager.audio.play_voice.assert_called_once_with(Sound.MENU_VOX_SENSITIVITY_LOW)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[5])
    async def test_start_on_sensitivity_plays_voice(self, handler, mock_state_manager):
        """START on the sensitivity option announces the new level (UC-012 step 5)."""
        handler.current_option = handler.option_names.index("sensitivity")
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "slow"
        mock_state_manager.game_settings_writer.get_flag_view.return_value = flag_view(SENSITIVITY_VALUES, "slow")

        await handler.handle_decrease_value("test_serial")

        mock_state_manager.audio.play_voice.assert_called_once_with(Sound.MENU_VOX_SENSITIVITY_HIGH)


class TestControllerHandlerProtocol:
    """Tests for the ControllerHandler protocol methods."""

    @pytest.mark.asyncio
    async def test_handle_button_delegates(self, handler):
        """handle_button should delegate to handle_button_event."""
        handler.handle_button_event = AsyncMock()
        await handler.handle_button("s1", "cross")
        handler.handle_button_event.assert_called_once_with("s1", "cross", None)

    @pytest.mark.asyncio
    async def test_on_enter_delegates(self, inactive_handler):
        """on_enter should delegate to enter."""
        inactive_handler.enter = AsyncMock()
        await inactive_handler.on_enter("s1")
        inactive_handler.enter.assert_called_once_with("s1")

    @pytest.mark.asyncio
    async def test_on_exit_delegates(self, handler):
        """on_exit for the admin-owning controller should delegate to exit."""
        handler.exit = AsyncMock()
        await handler.on_exit("test_serial")
        handler.exit.assert_called_once()

    def test_set_state_manager(self, inactive_handler, mock_state_manager):
        """set_state_manager should store the manager."""
        inactive_handler._state_manager = None
        inactive_handler.set_state_manager(mock_state_manager)
        assert inactive_handler._state_manager is mock_state_manager


class TestHelperProperties:
    """Tests for StateManager helper properties."""

    def test_current_game_mode_with_manager(self, handler, mock_state_manager):
        """Should return game mode name from state_manager."""
        assert handler._current_game_mode == "JoustFFA"

    def test_current_game_mode_without_manager(self, handler):
        """Should return 'JoustFFA' default without state_manager."""
        handler._state_manager = None
        assert handler._current_game_mode == "JoustFFA"


class TestAgentControlOption:
    """Tests for the on-device agent kill-switch admin option (#819)."""

    @pytest.mark.uc("UC-013", flow="main", steps=[1])
    def test_agent_control_in_option_cycle(self, handler):
        """agent_control must be a controller-cycled option with a color."""
        from services.menu.handlers.admin import AGENT_CONTROL_OPTION

        assert AGENT_CONTROL_OPTION in handler.option_names
        idx = handler.option_names.index(AGENT_CONTROL_OPTION)
        assert isinstance(handler.option_colors[idx], Colors)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="main", steps=[2, 3], br=["BR-001"])
    async def test_increase_routes_to_agent_control(self, handler, mock_state_manager):
        """SELECT on the agent option escalates policy, not a game.json write."""
        from services.menu.handlers.admin import AGENT_CONTROL_OPTION

        handler.current_option = handler.option_names.index(AGENT_CONTROL_OPTION)
        set_agent_level(mock_state_manager, "ambient")

        await handler.handle_increase_value("test_serial")

        # Wrote interventions_allowed in agent.json, escalating ambient -> standard.
        mock_state_manager.agent_settings_writer.update_default_variant.assert_called_once_with(
            "interventions_allowed", "standard"
        )
        # Did NOT touch the game settings writer.
        mock_state_manager.game_settings_writer.cycle_variant.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="main", steps=[2, 3])
    async def test_decrease_restricts_agent_policy(self, handler, mock_state_manager):
        """START on the agent option restricts policy one level toward none."""
        from services.menu.handlers.admin import AGENT_CONTROL_OPTION

        handler.current_option = handler.option_names.index(AGENT_CONTROL_OPTION)
        set_agent_level(mock_state_manager, "standard")

        await handler.handle_decrease_value("test_serial")

        # standard -> ambient (one step less permissive).
        mock_state_manager.agent_settings_writer.update_default_variant.assert_called_once_with(
            "interventions_allowed", "ambient"
        )

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="main", steps=[4])
    async def test_voice_still_plays_when_pulse_fails(self, handler, mock_state_manager):
        """A failing LED pulse must not drop the level voice (#1643)."""
        from services.menu.handlers.admin import AGENT_CONTROL_OPTION

        handler.current_option = handler.option_names.index(AGENT_CONTROL_OPTION)
        set_agent_level(mock_state_manager, "ambient")
        mock_state_manager.led.send_game_effect.side_effect = RuntimeError("stream gone")

        await handler.handle_increase_value("test_serial")

        mock_state_manager.audio.play_voice.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="main", steps=[3])
    async def test_panic_off_clamps_at_none(self, handler, mock_state_manager):
        """Restricting from 'none' stays at 'none' — the kill switch never wraps to 'full' (UC-013)."""
        handler.current_option = handler.option_names.index("agent_control")
        set_agent_level(mock_state_manager, "none")

        await handler.handle_decrease_value("test_serial")

        mock_state_manager.agent_settings_writer.update_default_variant.assert_called_once_with(
            "interventions_allowed", "none"
        )

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="main", steps=[3])
    async def test_full_clamps_at_full(self, handler, mock_state_manager):
        """Escalating from 'full' stays at 'full' — no wrap to 'none' (UC-013)."""
        handler.current_option = handler.option_names.index("agent_control")
        set_agent_level(mock_state_manager, "full")

        await handler.handle_increase_value("test_serial")

        mock_state_manager.agent_settings_writer.update_default_variant.assert_called_once_with(
            "interventions_allowed", "full"
        )

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="main", steps=[4])
    async def test_agent_control_led_and_voice_feedback(self, handler, mock_state_manager):
        """Cycling agent policy emits an LED pulse and a voice line."""
        from proto import controller_manager_pb2

        handler.current_option = handler.option_names.index("agent_control")
        set_agent_level(mock_state_manager, "ambient")

        await handler.handle_increase_value("test_serial")

        # LED pulse feedback was sent.
        mock_state_manager.led.send_game_effect.assert_called()
        effect = mock_state_manager.led.send_game_effect.call_args[0][1]
        assert effect == controller_manager_pb2.GAME_EFFECT_PULSE
        # Voice feedback was played.
        mock_state_manager.audio.play_voice.assert_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="main", steps=[4])
    @pytest.mark.parametrize(
        ("button", "start_level", "expected_color"),
        [
            ("decrease", "ambient", Colors.Red),  # none
            ("increase", "none", Colors.Turquoise),  # ambient
            ("increase", "ambient", Colors.Orange),  # standard
            ("increase", "standard", Colors.Green),  # full
        ],
    )
    async def test_agent_control_pulse_color_per_level(
        self, handler, mock_state_manager, button, start_level, expected_color
    ):
        """Each level pulses its own colour: none red, ambient turquoise, standard orange, full green."""
        handler.current_option = handler.option_names.index("agent_control")
        set_agent_level(mock_state_manager, start_level)

        if button == "increase":
            await handler.handle_increase_value("test_serial")
        else:
            await handler.handle_decrease_value("test_serial")

        assert mock_state_manager.led.send_game_effect.call_args.kwargs["color"] == expected_color.value

    @pytest.mark.uc("UC-013", flow="A3")
    @pytest.mark.asyncio
    async def test_write_failure_skips_feedback(self, handler, mock_state_manager):
        """A failed agent.json write must not emit feedback as if it succeeded."""
        handler.current_option = handler.option_names.index("agent_control")
        mock_state_manager.agent_settings_writer.update_default_variant.return_value = False

        await handler.handle_increase_value("test_serial")

        # No success feedback (level colour / level voice); only "Not saved" (#1746), cue: TestAgentWriteErrorCue.
        mock_state_manager.audio.play_voice.assert_awaited_once_with(Sound.MENU_VOX_ADMIN_NOT_SAVED)

    @pytest.mark.asyncio
    async def test_reads_agent_state_once(self, handler, mock_state_manager):
        """The level read is one flag-service GET, not one per variant lookup."""
        handler.current_option = handler.option_names.index("agent_control")
        set_agent_level(mock_state_manager, "standard")

        await handler.handle_increase_value("test_serial")

        mock_state_manager.agent_settings_writer.get_flag_view.assert_called_once_with("interventions_allowed")
        mock_state_manager.agent_settings_writer.update_default_variant.assert_called_once_with(
            "interventions_allowed", "full"
        )

    @pytest.mark.asyncio
    async def test_flag_service_calls_run_off_the_event_loop_thread(self, handler, mock_state_manager):
        """Blocking flag-service HTTP must not run on the event loop thread."""
        loop_thread = threading.get_ident()
        call_threads = []

        def read_view(*_args):
            call_threads.append(threading.get_ident())
            return flag_view(AGENT_LEVEL_VALUES, "ambient")

        def write(*_args):
            call_threads.append(threading.get_ident())
            return True

        handler.current_option = handler.option_names.index("agent_control")
        mock_state_manager.agent_settings_writer.get_flag_view.side_effect = read_view
        mock_state_manager.agent_settings_writer.update_default_variant.side_effect = write

        await handler.handle_increase_value("test_serial")

        assert len(call_threads) == 2
        assert loop_thread not in call_threads

    @pytest.mark.asyncio
    async def test_cycle_runs_off_the_event_loop_thread(self, handler, mock_state_manager):
        """The game-settings cycle (read + write) runs in a worker thread too."""
        loop_thread = threading.get_ident()
        call_threads = []

        def cycle(*_args, **_kwargs):
            call_threads.append(threading.get_ident())
            return "fast"

        handler.current_option = handler.option_names.index("sensitivity")
        mock_state_manager.game_settings_writer.cycle_variant.side_effect = cycle

        await handler.handle_increase_value("test_serial")

        assert call_threads and loop_thread not in call_threads

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="A2")
    async def test_no_agent_writer_is_safe(self, handler, mock_state_manager, caplog):
        """UC-013 A2: a missing agent writer logs a warning and no-ops rather than raising."""
        handler.current_option = handler.option_names.index("agent_control")
        mock_state_manager.agent_settings_writer = None

        with caplog.at_level(logging.WARNING, logger="services.menu.handlers.admin_options"):
            await handler.handle_increase_value("test_serial")

        assert any(r.levelname == "WARNING" and "Agent control unavailable" in r.getMessage() for r in caplog.records)
        mock_state_manager.led.send_game_effect.assert_not_called()
        mock_state_manager.audio.play_voice.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="A1")
    @pytest.mark.parametrize("off_ladder", ["probe", "shadow_experimental"])
    @pytest.mark.parametrize("button", ["increase", "decrease"])
    async def test_off_ladder_level_jumps_to_none(self, handler, mock_state_manager, off_ladder, button):
        """UC-013 A1: probe / shadow_experimental jump to none in either direction, with feedback."""
        from proto import controller_manager_pb2

        handler.current_option = handler.option_names.index("agent_control")
        set_agent_level(mock_state_manager, off_ladder)

        if button == "increase":
            await handler.handle_increase_value("test_serial")
        else:
            await handler.handle_decrease_value("test_serial")

        mock_state_manager.agent_settings_writer.update_default_variant.assert_called_once_with(
            "interventions_allowed", "none"
        )
        assert mock_state_manager.led.send_game_effect.call_args[0][1] == controller_manager_pb2.GAME_EFFECT_PULSE
        mock_state_manager.audio.play_voice.assert_called()

    @pytest.mark.uc("UC-013", flow="main", steps=[3])
    def test_next_agent_level_ladder_clamps(self):
        """_next_agent_level steps the 4-level ladder and clamps at both ends (UC-013)."""
        assert AdminModeHandler._next_agent_level("none", forward=True) == "ambient"
        assert AdminModeHandler._next_agent_level("ambient", forward=True) == "standard"
        assert AdminModeHandler._next_agent_level("standard", forward=True) == "full"
        assert AdminModeHandler._next_agent_level("full", forward=True) == "full"
        assert AdminModeHandler._next_agent_level("full", forward=False) == "standard"
        assert AdminModeHandler._next_agent_level("ambient", forward=False) == "none"
        assert AdminModeHandler._next_agent_level("none", forward=False) == "none"

    @pytest.mark.uc("UC-013", flow="A1")
    @pytest.mark.uc("UC-013", flow="A5", steps=[1])
    def test_next_agent_level_unknown_or_missing_goes_to_none(self):
        """UC-013 A1: an off-ladder value lands on none in either direction; A5 step 1: an unreadable
        level (None) restricts to none. The handler refuses Select from None before the ladder (A5 step 2)."""
        for current in ("bogus", "probe", "shadow_experimental"):
            assert AdminModeHandler._next_agent_level(current, forward=True) == "none"
            assert AdminModeHandler._next_agent_level(current, forward=False) == "none"
        assert AdminModeHandler._next_agent_level(None, forward=False) == "none"


class TestEffectiveValueDisplay:
    """Tests for the truthful admin value display, resolved through the flag service (#818, #2092)."""

    def test_effective_variant_reads_flag_service_not_lagging_client(self, handler, mock_state_manager):
        """The flag service's effective value wins over an in-process client one write behind (#2092)."""
        mock_state_manager.game_settings_writer.get_flag_view.return_value = flag_view(SENSITIVITY_VALUES, "ultra_fast")

        assert handler._effective_variant("sensitivity") == "ultra_fast"

    def test_effective_variant_reads_flag_state_once(self, handler, mock_state_manager):
        """One flag-service GET per display, not one per variant lookup."""
        handler._effective_variant("sensitivity")

        writer = mock_state_manager.game_settings_writer
        writer.get_flag_view.assert_called_once_with("sensitivity")
        writer.get_current_variant.assert_not_called()

    def test_effective_variant_flag_service_down(self, handler, mock_state_manager):
        """Flag service unreachable (view None) -> None, without raising."""
        mock_state_manager.game_settings_writer.get_flag_view.return_value = None

        assert handler._effective_variant("sensitivity") is None

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[3], br=["BR-006"])
    async def test_move_right_after_a_step_announces_the_new_value(self, handler, mock_state_manager):
        """Step 3 right after a step: the lagging in-process client must not announce the old value (#2092)."""
        mock_state_manager.game_settings_writer.get_flag_view.return_value = flag_view(SENSITIVITY_VALUES, "fast")
        handler.current_option = len(handler.option_names) - 1
        handler._show_value_feedback = AsyncMock()
        handler._play_value_voice = AsyncMock()

        await handler.handle_cycle_option("test_serial")

        handler._show_value_feedback.assert_awaited_once_with("test_serial", "sensitivity", 3)
        handler._play_value_voice.assert_awaited_once_with("sensitivity", 3)


class TestCycleFeedbackIsBaselineTransition:
    """The CYCLE feedback (span event + log) must describe the human-owned
    BASELINE transition the admin is actually making — NOT the agent's effective
    override (#818 reviewer SHOULD-FIX).

    Passive viewing of an option still shows the truthful effective value via
    _effective_variant (covered by TestEffectiveValueDisplay); these tests pin the
    distinct cycle path: old_variant = baseline being cycled FROM.
    """

    @staticmethod
    def _cycle_event(handler):
        """Return (name, attrs) of the last span add_event on the handler's span."""
        span = handler.tracer.start_as_current_span.return_value.__enter__.return_value
        assert span.add_event.called, "expected a span event for the cycle"
        name, kwargs = span.add_event.call_args
        # add_event(name, {attrs}) — attrs is the second positional arg.
        return name[0], name[1]

    @pytest.mark.asyncio
    async def test_sensitivity_cycle_old_variant_is_baseline_not_override(self, handler, mock_state_manager):
        """Agent overrode sensitivity to ultra_fast, baseline on disk is medium.

        Cycling writes baseline medium -> fast. The feedback MUST read
        'medium -> fast' (the baseline transition), never 'ultra_fast -> fast'.
        Under the old effective-as-old_variant behavior this would announce
        ultra_fast and fail.
        """
        mock_state_manager.game_settings_writer.get_current_variant.return_value = "medium"
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "fast"
        # Live flagd value differs (ultra_fast) -> _effective_variant would show it.

        await handler.handle_sensitivity("test_serial")

        _name, attrs = self._cycle_event(handler)
        assert attrs["old_variant"] == "medium"
        assert attrs["new_variant"] == "fast"
        # old_variant comes from get_current_variant (baseline), never the effective path.
        mock_state_manager.game_settings_writer.get_current_variant.assert_called()

    @pytest.mark.asyncio
    async def test_increase_value_old_variant_is_baseline_not_override(self, handler, mock_state_manager):
        """handle_increase_value cycle feedback is the baseline transition."""
        handler.current_option = handler.option_names.index("sensitivity")
        mock_state_manager.game_settings_writer.get_current_variant.return_value = "medium"
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "fast"

        await handler.handle_increase_value("test_serial")

        _name, attrs = self._cycle_event(handler)
        assert attrs["old_variant"] == "medium"
        assert attrs["new_variant"] == "fast"

    @pytest.mark.asyncio
    async def test_decrease_value_old_variant_is_baseline_not_override(self, handler, mock_state_manager):
        """handle_decrease_value cycle feedback is the baseline transition."""
        handler.current_option = handler.option_names.index("sensitivity")
        mock_state_manager.game_settings_writer.get_current_variant.return_value = "medium"
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "slow"

        await handler.handle_decrease_value("test_serial")

        _name, attrs = self._cycle_event(handler)
        assert attrs["old_variant"] == "medium"
        assert attrs["new_variant"] == "slow"


class TestForceStartThresholdFlag:
    """The trigger-hold force-start threshold reads from the game.json
    ``force_all_start_seconds`` flag (#1215), migrated from the
    ADMIN_FORCE_START_SECONDS env var. Read at USE TIME (not cached at
    ``__init__``) so it resolves against live flagd once a user holds the
    trigger, rather than fail-open before flagd is reachable (#1177).
    """

    def _build(self, mock_tracer, mock_callbacks, mock_metrics):
        return AdminModeHandler(
            controller_channel=create_autospec(grpc.aio.Channel, instance=True),
            tracer=mock_tracer,
            callbacks=mock_callbacks,
            metrics=mock_metrics,
        )

    @pytest.mark.uc("UC-014", flow="main", steps=[2])
    def test_threshold_read_from_flag(self, mock_tracer, mock_callbacks, mock_metrics):
        handler = self._build(mock_tracer, mock_callbacks, mock_metrics)
        with patch("services.menu.handlers.admin.read_float_flag", return_value=0.6) as mock_read:
            assert handler._force_start_threshold() == 0.6
        mock_read.assert_called_once_with("game", "force_all_start_seconds", 3.0)

    def test_threshold_fails_open_to_default(self, mock_tracer, mock_callbacks, mock_metrics):
        """When flagd is unreachable/undefined (#1177), read_float_flag
        returns the supplied 3.0 default — the real-play hold duration."""
        handler = self._build(mock_tracer, mock_callbacks, mock_metrics)
        with patch(
            "services.menu.handlers.admin.read_float_flag",
            side_effect=lambda _domain, _key, default, _game_id=None: default,
        ):
            assert handler._force_start_threshold() == 3.0


@pytest.mark.uc("UC-012", br=["BR-004"])
class TestSingleAdminSession:
    """UC-012 Pre-1 and BR-004: admin mode needs a RUNNING menu and only one controller holds it.

    Drives the real MenuServicer.on_button combo detection, the real StateManager
    and the real AdminModeHandler — no re-implemented logic.
    """

    FACE_BUTTONS = ["cross", "circle", "square", "triangle"]

    @pytest.fixture
    def stack(self, mock_tracer, mock_callbacks, mock_metrics):
        from types import SimpleNamespace

        from services.menu.state_manager import StateManager

        led = create_autospec(LedController, instance=True)
        led.send_game_effect.return_value = True
        led.send_base_color.return_value = True
        audio = create_autospec(AudioHelper, instance=True)
        state_manager = StateManager(
            led,
            audio,
            create_autospec(FlagServiceWriter, instance=True),
            create_autospec(FlagServiceWriter, instance=True),
            AsyncMock(),
        )

        connected_handler = create_autospec(ControllerHandler, instance=True)
        connected_handler.state = ControllerState.CONNECTED
        state_manager.register_handler(connected_handler)

        admin_handler = AdminModeHandler(
            controller_channel=create_autospec(grpc.aio.Channel, instance=True),
            tracer=mock_tracer,
            callbacks=mock_callbacks,
            metrics=mock_metrics,
        )
        state_manager.register_handler(admin_handler)

        servicer = SimpleNamespace(
            idle_monitor=create_autospec(IdleMonitor, instance=True),
            state_manager=state_manager,
            admin_handler=admin_handler,
            state=menu_pb2.MenuState.RUNNING,
        )
        return servicer, state_manager, admin_handler

    @staticmethod
    def _frozen_admin_clock():
        """Freeze the handler's wall clock so only the timer task can time the session out.

        With a 10 ms timeout, the wall-clock fallback in handle_button_event (#1782) fires
        on a slow runner while the combo press is still being dispatched: the session ends
        inside _press_combo and _timeout_task is already None when the test awaits it.
        """
        fake_time = create_autospec(time, instance=False)
        fake_time.time.return_value = 1000.0
        return patch("services.menu.handlers.admin.time", fake_time)

    async def _press_combo(self, servicer, serial):
        from services.menu.servicer import MenuServicer

        for button in self.FACE_BUTTONS:
            await MenuServicer.on_button(servicer, serial, button, True)
        for button in self.FACE_BUTTONS:
            await MenuServicer.on_button(servicer, serial, button, False)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", cond=["Pre-1"])
    async def test_second_controller_combo_is_rejected(self, stack):
        """A second controller completing the combo stays CONNECTED; the first keeps its session."""
        servicer, state_manager, admin_handler = stack
        try:
            await self._press_combo(servicer, "A")
            assert state_manager.get_controller_state("A") == ControllerState.ADMIN
            timeout_task = admin_handler._timeout_task

            await self._press_combo(servicer, "B")

            assert state_manager.get_controller_state("B") == ControllerState.CONNECTED
            assert state_manager.get_controller_state("A") == ControllerState.ADMIN
            assert admin_handler.active is True
            assert admin_handler.controller_serial == "A"
            # The first controller's auto-exit timeout must not be cancelled/replaced.
            assert admin_handler._timeout_task is timeout_task
            assert not timeout_task.done()
            # BR-004: the rejected combo press is handled as an ordinary button of B.
            connected_handler = state_manager._handlers[ControllerState.CONNECTED]
            assert call("B", "triangle", None) in connected_handler.handle_button.await_args_list
        finally:
            await admin_handler.exit()

    @pytest.mark.asyncio
    async def test_second_controller_can_enter_after_first_exits(self, stack):
        """Once the active admin leaves, another controller may enter admin mode."""
        servicer, state_manager, admin_handler = stack
        try:
            await self._press_combo(servicer, "A")
            await state_manager.transition_to("A", ControllerState.CONNECTED)
            assert admin_handler.active is False

            await self._press_combo(servicer, "B")

            assert state_manager.get_controller_state("B") == ControllerState.ADMIN
            assert admin_handler.controller_serial == "B"
        finally:
            await admin_handler.exit()

    @pytest.mark.asyncio
    async def test_buttons_from_non_admin_serial_are_ignored(self, inactive_handler, mock_state_manager):
        """Defence in depth: admin commands are only accepted from the admin controller."""
        mock_state_manager.get_controller_state = MagicMock(return_value=ControllerState.ADMIN)
        inactive_handler.active = True
        inactive_handler.controller_serial = "A"
        inactive_handler.entry_time = time.time()
        inactive_handler._exit_to_connected = AsyncMock()

        await inactive_handler.handle_button_event("B", ButtonTrackingKey.PS)

        inactive_handler._exit_to_connected.assert_not_awaited()
        assert inactive_handler.active is True

    @pytest.mark.asyncio
    async def test_on_exit_for_other_serial_keeps_session(self, inactive_handler):
        """on_exit for a controller that does not own the session must not end it."""
        inactive_handler.active = True
        inactive_handler.controller_serial = "A"
        inactive_handler.entry_time = time.time()

        await inactive_handler.on_exit("B")

        assert inactive_handler.active is True
        assert inactive_handler.controller_serial == "A"

    # --- Lock release (review of PR #1373): the lock must never go stale. ---

    async def _assert_second_controller_can_enter(self, servicer, state_manager, admin_handler):
        await self._press_combo(servicer, "B")
        assert state_manager.get_controller_state("B") == ControllerState.ADMIN
        assert admin_handler.active is True
        assert admin_handler.controller_serial == "B"

    @pytest.mark.asyncio
    async def test_lobby_reset_releases_lock(self, stack):
        """StateManager.reset() (lobby restart) releases the admin session."""
        servicer, state_manager, admin_handler = stack
        try:
            await self._press_combo(servicer, "A")
            timeout_task = admin_handler._timeout_task

            await state_manager.reset()

            assert admin_handler.active is False
            await asyncio.sleep(0)
            assert timeout_task.cancelled()
            await self._assert_second_controller_can_enter(servicer, state_manager, admin_handler)
        finally:
            await admin_handler.exit()

    @pytest.mark.asyncio
    async def test_stream_reconnect_releases_lock(self, stack):
        """on_stream_reconnected() releases the admin session."""
        servicer, state_manager, admin_handler = stack
        try:
            await self._press_combo(servicer, "A")

            await state_manager.on_stream_reconnected()
            await state_manager.on_controller_connected("A")

            assert admin_handler.active is False
            await self._assert_second_controller_can_enter(servicer, state_manager, admin_handler)
        finally:
            await admin_handler.exit()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A11")
    async def test_stop_menu_releases_lock(self, stack):
        """StopMenu releases the admin session."""
        from proto import menu_pb2
        from services.menu.servicer import MenuServicer

        servicer, state_manager, admin_handler = stack
        servicer.state = menu_pb2.MenuState.RUNNING
        servicer._clear_ready_state = MagicMock()
        servicer.event_publisher = MagicMock(publish=AsyncMock())
        try:
            await self._press_combo(servicer, "A")

            with patch("services.menu.servicer.metrics"):
                response = await MenuServicer.StopMenu(servicer, MagicMock(), MagicMock())

            assert response.success is True
            assert admin_handler.active is False
            servicer.state = menu_pb2.MenuState.RUNNING  # StartMenu again (Pre-1)
            await self._assert_second_controller_can_enter(servicer, state_manager, admin_handler)
        finally:
            await admin_handler.exit()

    @pytest.mark.asyncio
    async def test_admin_disconnect_releases_lock(self, stack):
        """The admin controller disconnecting while in ADMIN releases the lock."""
        servicer, state_manager, admin_handler = stack
        try:
            await self._press_combo(servicer, "A")

            await state_manager.on_controller_disconnected("A")

            assert admin_handler.active is False
            await self._assert_second_controller_can_enter(servicer, state_manager, admin_handler)
        finally:
            await admin_handler.exit()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A4")
    async def test_timeout_releases_lock(self, stack):
        """The auto-exit timeout releases the lock and returns the admin to CONNECTED."""
        servicer, state_manager, admin_handler = stack
        admin_handler._timeout_seconds = 0.01
        try:
            with self._frozen_admin_clock():
                await self._press_combo(servicer, "A")
                timer = admin_handler._timeout_task
                assert timer is not None
                await timer

                assert admin_handler.active is False
                assert state_manager.get_controller_state("A") == ControllerState.CONNECTED
                # B's combo also runs with the 10 ms timeout: keep the clock frozen.
                await self._assert_second_controller_can_enter(servicer, state_manager, admin_handler)
        finally:
            await admin_handler.exit()

    @pytest.mark.asyncio
    async def test_timeout_ends_session_when_owner_state_already_cleared(self, stack):
        """If the owner's state was cleared behind the handler's back, the timeout still ends the session."""
        servicer, state_manager, admin_handler = stack
        admin_handler._timeout_seconds = 0.01
        try:
            with self._frozen_admin_clock():
                await self._press_combo(servicer, "A")
                timer = admin_handler._timeout_task
                assert timer is not None
                # Simulate a clear that bypasses on_exit and the explicit abandon hooks.
                state_manager.controller_states["A"] = ControllerState.CONNECTED
                await timer

            assert admin_handler.active is False
        finally:
            await admin_handler.exit()

    @pytest.mark.asyncio
    async def test_stale_session_does_not_block_new_admin(self, stack):
        """Self-healing lock: a session whose owner is no longer ADMIN is released on the next combo."""
        servicer, state_manager, admin_handler = stack
        try:
            await self._press_combo(servicer, "A")
            stale_timeout = admin_handler._timeout_task
            # Clear state behind the handler's back (no on_exit, no abandon hook).
            state_manager.controller_states["A"] = ControllerState.CONNECTED

            await self._assert_second_controller_can_enter(servicer, state_manager, admin_handler)
            await asyncio.sleep(0)
            assert stale_timeout.cancelled()
        finally:
            await admin_handler.exit()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", cond=["Pre-1"])
    @pytest.mark.parametrize("menu_state", ["STOPPED", "GAME_STARTING"])
    async def test_combo_outside_a_running_menu_is_ignored(self, stack, menu_state):
        """The combo enters admin mode only while the menu is RUNNING; otherwise it is an ordinary press."""
        servicer, state_manager, admin_handler = stack
        servicer.state = menu_pb2.MenuState.Value(menu_state)
        try:
            await self._press_combo(servicer, "A")

            assert state_manager.get_controller_state("A") == ControllerState.CONNECTED
            assert admin_handler.active is False
            connected_handler = state_manager._handlers[ControllerState.CONNECTED]
            assert call("A", "triangle", None) in connected_handler.handle_button.await_args_list
        finally:
            await admin_handler.exit()


class TestAgentWriteErrorCue:
    """UC-013 A3 error cue (#1705), A4 and BR-001 at handler level (#1725)."""

    @staticmethod
    def _select_agent_control(handler, manager, level="ambient"):
        handler.current_option = handler.option_names.index("agent_control")
        set_agent_level(manager, level)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="A3", steps=[2])
    async def test_refused_write_gives_error_pulse_only(self, handler, mock_state_manager):
        """A refused or unreachable write pulses red 400 ms at speed 10, with no sound."""
        from proto import controller_manager_pb2

        self._select_agent_control(handler, mock_state_manager)
        mock_state_manager.agent_settings_writer.update_default_variant.return_value = False

        await handler.handle_increase_value("test_serial")

        mock_state_manager.led.send_game_effect.assert_called_once()
        call = mock_state_manager.led.send_game_effect.call_args
        assert call[0][1] == controller_manager_pb2.GAME_EFFECT_PULSE
        assert call.kwargs["color"] == Colors.Red.value
        assert call.kwargs["duration_ms"] == 400
        assert call.kwargs["speed"] == 10
        mock_state_manager.audio.play_sound.assert_not_called()
        # Only the "Not saved" line (#1746), never a level voice line: it must not read as a level change.
        mock_state_manager.audio.play_voice.assert_awaited_once_with(Sound.MENU_VOX_ADMIN_NOT_SAVED)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="A3", steps=[2])
    async def test_raising_write_gives_error_pulse_only(self, handler, mock_state_manager):
        """An unexpected exception from the agent write gives the same red 400 ms pulse, no voice."""
        self._select_agent_control(handler, mock_state_manager)
        mock_state_manager.agent_settings_writer.update_default_variant.side_effect = RuntimeError("boom")

        await handler.handle_increase_value("test_serial")

        mock_state_manager.led.send_game_effect.assert_called_once()
        assert mock_state_manager.led.send_game_effect.call_args.kwargs["duration_ms"] == 400
        mock_state_manager.audio.play_voice.assert_awaited_once_with(Sound.MENU_VOX_ADMIN_NOT_SAVED)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="main", steps=[4])
    async def test_successful_write_gives_no_error_cue(self, handler, mock_state_manager):
        """The success path plays the level voice and no error cue."""
        self._select_agent_control(handler, mock_state_manager)
        mock_state_manager.agent_settings_writer.update_default_variant.return_value = True

        await handler.handle_increase_value("test_serial")

        mock_state_manager.audio.play_voice.assert_awaited_once()
        mock_state_manager.audio.play_sound.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="A4", steps=[2])
    async def test_applied_but_not_persisted_continues_with_level_feedback(self, handler, mock_state_manager):
        """Applied-but-unpersisted counts as True: step 4 feedback, no error cue."""
        from lib.types import Sound

        self._select_agent_control(handler, mock_state_manager, "ambient")
        mock_state_manager.agent_settings_writer.update_default_variant.return_value = True  # applied, NOT persisted

        await handler.handle_increase_value("test_serial")

        assert mock_state_manager.led.send_game_effect.call_args.kwargs["color"] == Colors.Orange.value
        mock_state_manager.audio.play_voice.assert_awaited_once_with(Sound.MENU_VOX_ADMINOP_2)
        mock_state_manager.audio.play_sound.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", br=["BR-001"])
    @pytest.mark.parametrize("button", ["increase", "decrease"])
    async def test_only_interventions_allowed_is_written(self, handler, mock_state_manager, button):
        """The menu writes interventions_allowed and no other flag, in any writer."""
        self._select_agent_control(handler, mock_state_manager, "standard")

        if button == "increase":
            await handler.handle_increase_value("test_serial")
        else:
            await handler.handle_decrease_value("test_serial")

        writer = mock_state_manager.agent_settings_writer
        assert [c.args[0] for c in writer.update_default_variant.call_args_list] == ["interventions_allowed"]
        writer.cycle_variant.assert_not_called()
        for other in (mock_state_manager.game_settings_writer, mock_state_manager.user_prefs_writer):
            other.update_default_variant.assert_not_called()
            other.cycle_variant.assert_not_called()


class _AgentFlagService:
    """A fake flag service for agent/interventions_allowed (#2095), driven through the real FlagServiceWriter.

    ``slow_put``: a PUT applies (``applies``) but answers after the client deadline (TimeoutError).
    ``get_down``: every GET fails, a PUT still lands. ``reread_down``: the GET after a timed-out PUT fails.
    """

    def __init__(self, level: str):
        self.level = level
        self.puts: list[str] = []
        self.slow_put = False
        self.applies = True
        self.get_down = False
        self.reread_down = False
        self._timed_out = False

    def __call__(self, method, url, body):
        if method == "PUT":
            self.puts.append(body["variant"])
            if self.slow_put:
                if self.applies:
                    self.level = body["variant"]
                self._timed_out = True
                raise TimeoutError("timed out")
            self.level = body["variant"]
            return 200, {"variant": body["variant"], "persisted": True}
        if self.get_down or (self._timed_out and self.reread_down):
            raise TimeoutError("timed out")
        return 200, {
            "variants": list(AGENT_LEVEL_VALUES),
            "variant_values": dict(AGENT_LEVEL_VALUES),
            "effective_default": self.level,
        }


class TestAgentLadderAgainstTheFlagService:
    """UC-013 A3, A5, BR-003 (#2095): the ladder steps from the flag service's own view, and reports truthfully.

    The real FlagServiceWriter runs over a fake flag service; flagd (the in-process client) keeps serving a
    stale level, as it does while a write propagates (NFR-050).
    """

    @staticmethod
    def _wire(handler, manager, service: _AgentFlagService, stale_flagd_level: str):
        from lib.flag_service_writer import FlagServiceWriter

        handler.current_option = handler.option_names.index("agent_control")
        manager.agent_settings_writer = FlagServiceWriter("agent", "http://flag-service:8070", transport=service)
        manager.agent_settings_client.get_string_value.return_value = AGENT_LEVEL_VALUES[stale_flagd_level]

    @staticmethod
    def _error_cue_shown(manager) -> bool:
        return any(c.kwargs.get("duration_ms") == 400 for c in manager.led.send_game_effect.call_args_list)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="A3", br=["BR-003"])
    @pytest.mark.uc("UC-013", flow="main", steps=[3])
    async def test_second_press_steps_from_a_timed_out_write_that_applied(self, handler, mock_state_manager):
        """Start, Start from standard: the first PUT times out but lands, flagd still serves standard; the
        second press steps ambient -> none, not standard -> ambient again."""
        service = _AgentFlagService("standard")
        self._wire(handler, mock_state_manager, service, stale_flagd_level="standard")

        service.slow_put = True
        await handler.handle_decrease_value("test_serial")
        service.slow_put = False
        await handler.handle_decrease_value("test_serial")

        assert service.puts == ["ambient", "none"]
        assert service.level == "none"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="A3")
    @pytest.mark.uc("UC-013", flow="main", steps=[4])
    async def test_timed_out_write_that_applied_gives_level_feedback_not_the_error_cue(
        self, handler, mock_state_manager
    ):
        """Truthful report: the re-read shows the level landed, so the host sees and hears the new level."""
        service = _AgentFlagService("standard")
        service.slow_put = True
        self._wire(handler, mock_state_manager, service, stale_flagd_level="standard")

        await handler.handle_decrease_value("test_serial")

        assert not self._error_cue_shown(mock_state_manager)
        assert mock_state_manager.led.send_game_effect.call_args.kwargs["color"] == Colors.Turquoise.value
        mock_state_manager.audio.play_voice.assert_awaited_once_with(Sound.MENU_VOX_ADMINOP_1)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="A3")
    @pytest.mark.parametrize("outcome", ["not_applied", "reread_failed"])
    async def test_timed_out_write_not_confirmed_gives_the_error_cue(self, handler, mock_state_manager, outcome):
        """Truthful report: a timed-out write the re-read does not confirm gets the error cue, no level voice."""
        service = _AgentFlagService("standard")
        service.slow_put = True
        service.applies = outcome != "not_applied"
        service.reread_down = outcome == "reread_failed"
        self._wire(handler, mock_state_manager, service, stale_flagd_level="standard")

        await handler.handle_decrease_value("test_serial")

        assert self._error_cue_shown(mock_state_manager)
        mock_state_manager.audio.play_voice.assert_awaited_once_with(Sound.MENU_VOX_ADMIN_NOT_SAVED)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="A5", steps=[1])
    async def test_restrict_with_the_level_unreadable_still_writes_none(self, handler, mock_state_manager):
        """Kill direction fails closed: Start with the flag service unreadable still writes none."""
        service = _AgentFlagService("full")
        service.get_down = True
        self._wire(handler, mock_state_manager, service, stale_flagd_level="full")

        await handler.handle_decrease_value("test_serial")

        assert service.puts == ["none"]
        assert service.level == "none"
        mock_state_manager.audio.play_voice.assert_awaited_once_with(Sound.MENU_VOX_ADMINOP_OFF)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="A3")
    @pytest.mark.parametrize("applies", [True, False])
    @pytest.mark.parametrize("put_error", ["malformed_json", "broken_response"])
    async def test_malformed_put_answer_is_settled_by_a_reread(self, handler, mock_state_manager, put_error, applies):
        """A PUT whose answer is malformed or broken has an unknown outcome: the re-read decides,
        so the host gets the level feedback or the error cue, never silence."""
        import http.client

        service = _AgentFlagService("standard")
        self._wire(handler, mock_state_manager, service, stale_flagd_level="standard")
        error = ValueError("Expecting value") if put_error == "malformed_json" else http.client.IncompleteRead(b"")

        def transport(method, url, body):
            if method == "PUT":
                service.puts.append(body["variant"])
                if applies:
                    service.level = body["variant"]
                raise error
            return service(method, url, body)

        mock_state_manager.agent_settings_writer._transport = transport

        await handler.handle_decrease_value("test_serial")

        assert service.puts == ["ambient"]
        if applies:
            assert not self._error_cue_shown(mock_state_manager)
            mock_state_manager.audio.play_voice.assert_awaited_once_with(Sound.MENU_VOX_ADMINOP_1)
        else:
            assert self._error_cue_shown(mock_state_manager)
            mock_state_manager.audio.play_voice.assert_awaited_once_with(Sound.MENU_VOX_ADMIN_NOT_SAVED)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="A5", steps=[1])
    @pytest.mark.parametrize("read_error", ["malformed_json", "broken_response"])
    async def test_restrict_with_a_malformed_read_still_writes_none(self, handler, mock_state_manager, read_error):
        """A read that fails in any way (a malformed body, a broken HTTP response) is unreadable: Start writes none."""
        import http.client

        service = _AgentFlagService("full")
        self._wire(handler, mock_state_manager, service, stale_flagd_level="full")
        error = ValueError("Expecting value") if read_error == "malformed_json" else http.client.IncompleteRead(b"")

        def transport(method, url, body):
            if method == "GET":
                raise error
            return service(method, url, body)

        mock_state_manager.agent_settings_writer._transport = transport

        await handler.handle_decrease_value("test_serial")

        assert service.puts == ["none"]
        mock_state_manager.audio.play_voice.assert_awaited_once_with(Sound.MENU_VOX_ADMINOP_OFF)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="A5", steps=[2])
    async def test_escalate_with_the_level_unreadable_is_refused(self, handler, mock_state_manager):
        """Select with the flag service unreadable writes nothing and gives the error cue only."""
        service = _AgentFlagService("ambient")
        service.get_down = True
        self._wire(handler, mock_state_manager, service, stale_flagd_level="ambient")

        await handler.handle_increase_value("test_serial")

        assert service.puts == []
        assert self._error_cue_shown(mock_state_manager)
        mock_state_manager.audio.play_voice.assert_not_called()


class TestOptionAnnouncementUsesEffectiveValue:
    """D-M6 (#1739): selecting an option announces the EFFECTIVE value (UC-012 step 3, BR-006)."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[3], br=["BR-006"])
    async def test_selecting_sensitivity_announces_agent_override(self, handler, mock_state_manager):
        """Baseline medium, agent override ultra_fast: colour and voice are ultra_fast's."""
        handler.current_option = len(handler.option_names) - 1  # next Move press wraps to sensitivity
        mock_state_manager.game_settings_writer.get_flag_view.return_value = flag_view(SENSITIVITY_VALUES, "ultra_fast")
        handler._show_value_feedback = AsyncMock()
        handler._play_value_voice = AsyncMock()

        await handler.handle_cycle_option("test_serial")

        assert handler.option_names[handler.current_option] == "sensitivity"
        handler._show_value_feedback.assert_awaited_once_with("test_serial", "sensitivity", 4)
        handler._play_value_voice.assert_awaited_once_with("sensitivity", 4)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[3], br=["BR-006"])
    async def test_agent_control_is_not_announced_as_a_value(self, handler):
        handler.current_option = handler.option_names.index("force_all_start")
        handler._show_value_feedback = AsyncMock()
        handler._play_value_voice = AsyncMock()

        await handler.handle_cycle_option("test_serial")

        assert handler.option_names[handler.current_option] == "agent_control"
        handler._show_value_feedback.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[3], br=["BR-006"])
    async def test_selecting_num_teams_announces_effective_value(self, handler, mock_state_manager):
        mock_state_manager.game_settings_writer.get_flag_view.return_value = flag_view(
            {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}, "four"
        )
        handler.current_option = handler.option_names.index("sensitivity")
        handler._show_value_feedback = AsyncMock()
        handler._play_value_voice = AsyncMock()

        await handler.handle_cycle_option("test_serial")

        handler._show_value_feedback.assert_awaited_once_with("test_serial", "num_teams", 4)
        handler._play_value_voice.assert_awaited_once_with("num_teams", 4)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[3], br=["BR-006"])
    async def test_selecting_force_all_start_announces_effective_value(self, handler, mock_state_manager):
        mock_state_manager.game_settings_writer.get_flag_view.return_value = flag_view({"on": True, "off": False}, "on")
        handler.current_option = handler.option_names.index("num_teams")
        handler._show_value_feedback = AsyncMock()
        handler._play_value_voice = AsyncMock()

        await handler.handle_cycle_option("test_serial")

        handler._show_value_feedback.assert_awaited_once_with("test_serial", "force_all_start", True)
        handler._play_value_voice.assert_awaited_once_with("force_all_start", True)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[3], br=["BR-006"])
    async def test_selecting_announces_the_flag_service_value(self, handler, mock_state_manager):
        mock_state_manager.game_settings_writer.get_flag_view.return_value = flag_view(SENSITIVITY_VALUES, "fast")
        handler.current_option = len(handler.option_names) - 1
        handler._show_value_feedback = AsyncMock()
        handler._play_value_voice = AsyncMock()

        await handler.handle_cycle_option("test_serial")

        handler._show_value_feedback.assert_awaited_once_with("test_serial", "sensitivity", 3)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[3], br=["BR-006"])
    async def test_selecting_with_flag_service_down_is_silent(self, handler, mock_state_manager):
        mock_state_manager.game_settings_writer.get_flag_view.return_value = None
        handler.current_option = len(handler.option_names) - 1
        handler._show_value_feedback = AsyncMock()
        handler._play_value_voice = AsyncMock()

        await handler.handle_cycle_option("test_serial")

        handler._show_value_feedback.assert_not_awaited()
        handler._play_value_voice.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[4, 5], br=["BR-006"])
    async def test_step_with_overlay_announces_baseline_then_agent_cue_and_effective(self, handler, mock_state_manager):
        """Baseline steps to fast; agent overlay holds ultra_fast: new setting, magenta cue, effective value."""
        handler.current_option = handler.option_names.index("sensitivity")
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "fast"
        mock_state_manager.game_settings_writer.get_flag_view.return_value = flag_view(SENSITIVITY_VALUES, "ultra_fast")
        order = []

        async def show(_serial, _option, value):
            order.append(("show", value))

        async def voice(_option, value):
            order.append(("voice", value))

        async def effect(*_args, **kwargs):
            order.append(("cue", kwargs.get("color")))

        handler._show_value_feedback = show
        handler._play_value_voice = voice

        async def say(sound):
            order.append(("say", sound))

        handler._send_game_effect = effect
        handler._play_voice = say

        await handler.handle_increase_value("test_serial")
        await asyncio.gather(*handler._voice_tasks)  # the "Agent" line is detached (may overlap the value LED)

        assert order[:3] == [("show", 3), ("voice", 3), ("cue", Colors.Magenta.value)]
        # #1746: the spoken "Agent" reinforces the magenta pulse; the value LED never waits for it
        assert order.index(("say", Sound.MENU_VOX_ADMIN_AGENT)) > 2
        assert order.index(("show", 4)) < order.index(("voice", 4))
        assert {("say", Sound.MENU_VOX_ADMIN_AGENT), ("show", 4), ("voice", 4)} <= set(order[3:])

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[4, 5], br=["BR-006"])
    async def test_slow_agent_voice_does_not_delay_the_effective_value_led(self, handler, mock_state_manager):
        """A hanging audio RPC for the "Agent" line must not hold back the effective-value LED."""
        handler.current_option = handler.option_names.index("sensitivity")
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "fast"
        mock_state_manager.game_settings_writer.get_flag_view.return_value = flag_view(SENSITIVITY_VALUES, "ultra_fast")
        release = asyncio.Event()
        shown = []

        async def slow_voice(_sound):
            await release.wait()

        async def show(_serial, _option, value):
            shown.append(value)

        handler._play_voice = slow_voice
        handler._show_value_feedback = show
        handler._play_value_voice = AsyncMock()
        handler._send_game_effect = AsyncMock()

        await asyncio.wait_for(handler.handle_increase_value("test_serial"), timeout=1)

        assert shown == [3, 4]  # effective-value LED sent while the Agent voice is still pending
        release.set()
        await asyncio.gather(*handler._voice_tasks)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[4, 5], br=["BR-006"])
    async def test_step_without_overlay_announces_only_baseline(self, handler, mock_state_manager):
        handler.current_option = handler.option_names.index("sensitivity")
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "fast"
        mock_state_manager.game_settings_writer.get_flag_view.return_value = flag_view(SENSITIVITY_VALUES, "fast")
        handler._show_value_feedback = AsyncMock()
        handler._play_value_voice = AsyncMock()
        handler._send_game_effect = AsyncMock()

        await handler.handle_decrease_value("test_serial")

        handler._show_value_feedback.assert_awaited_once_with("test_serial", "sensitivity", 3)
        handler._send_game_effect.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="main", steps=[4, 5], br=["BR-006"])
    @pytest.mark.parametrize("option", ["sensitivity", "num_teams"])
    async def test_step_with_lagging_flagd_client_announces_once(self, handler, mock_state_manager, option):
        """The write applied per the flag service (#2092): one announcement, no cue."""
        from services.menu.settings_ops import _VARIANT_VALUES

        handler.current_option = handler.option_names.index(option)
        values = _VARIANT_VALUES[option]
        new = list(values)[2]
        writer = mock_state_manager.game_settings_writer
        writer.cycle_variant.return_value = new
        writer.get_flag_view.return_value = flag_view(values, new)  # flag service: the write is applied
        handler._show_value_feedback = AsyncMock()
        handler._play_value_voice = AsyncMock()
        handler._send_game_effect = AsyncMock()

        await handler.handle_increase_value("test_serial")

        handler._show_value_feedback.assert_awaited_once_with("test_serial", option, values[new])
        handler._play_value_voice.assert_awaited_once_with(option, values[new])
        handler._send_game_effect.assert_not_awaited()


class TestWriteFailedVoice:
    """#1746: the "Not saved" line reinforces the red error pulse on a failed write, never on a failed read."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A5", steps=[2])
    async def test_failed_value_write_pulses_red_then_says_not_saved(self, handler, mock_state_manager):
        handler.current_option = handler.option_names.index("sensitivity")
        mock_state_manager.game_settings_writer.cycle_variant.return_value = None

        await handler.handle_increase_value("test_serial")

        assert mock_state_manager.led.send_game_effect.call_args.kwargs["color"] == Colors.Red.value
        mock_state_manager.audio.play_voice.assert_awaited_once_with(Sound.MENU_VOX_ADMIN_NOT_SAVED)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A5", steps=[2])
    @pytest.mark.parametrize(
        ("handler_method", "writer_name"),
        [("handle_sensitivity", "game_settings_writer"), ("handle_instructions", "user_prefs_writer")],
    )
    async def test_failed_circle_and_square_write_pulses_red_then_says_not_saved(
        self, handler, mock_state_manager, handler_method, writer_name
    ):
        getattr(mock_state_manager, writer_name).cycle_variant.return_value = None

        await getattr(handler, handler_method)("test_serial")

        assert mock_state_manager.led.send_game_effect.call_args.kwargs["color"] == Colors.Red.value
        mock_state_manager.audio.play_voice.assert_awaited_once_with(Sound.MENU_VOX_ADMIN_NOT_SAVED)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-012", flow="A7", steps=[1])
    async def test_unreadable_flag_service_stays_led_only(self, handler, mock_state_manager):
        handler.current_option = handler.option_names.index("sensitivity")
        mock_state_manager.game_settings_writer.get_flag_view.return_value = None

        await handler._announce_effective_value("test_serial", "sensitivity")

        assert mock_state_manager.led.send_game_effect.call_args.kwargs["color"] == Colors.Red.value
        mock_state_manager.audio.play_voice.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-013", flow="A3", steps=[2])
    async def test_voice_failure_does_not_break_the_error_cue(self, handler, mock_state_manager):
        handler.current_option = handler.option_names.index("agent_control")
        set_agent_level(mock_state_manager, "ambient")
        mock_state_manager.agent_settings_writer.update_default_variant.return_value = False
        mock_state_manager.audio.play_voice.side_effect = RuntimeError("audio down")

        await handler.handle_increase_value("test_serial")

        mock_state_manager.led.send_game_effect.assert_called_once()


class TestForceStartHoldTimerAndRefusal:
    """#2377: the hold timer dies with the session; a refused start restores the menu state."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="A1")
    @pytest.mark.parametrize("leave", ["exit", "abandon"])
    async def test_leaving_admin_cancels_trigger_hold_timer(self, handler, leave):
        """B1: trigger press, leave admin, re-enter within the threshold: no force start."""
        handler.handle_force_start = AsyncMock()
        with patch.object(handler, "_force_start_threshold", return_value=0.05):
            await handler._start_trigger_hold_tracking("test_serial")
        timer = handler._trigger_hold_task

        if leave == "exit":
            await handler.exit()
        else:
            handler.abandon_session()
        # Re-enter as the same admin before the threshold elapses.
        handler.active = True
        handler.controller_serial = "test_serial"
        await asyncio.sleep(0.15)

        assert timer.done()
        assert handler._trigger_hold_task is None
        handler.handle_force_start.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="main", steps=[5])
    async def test_force_start_exit_does_not_cancel_its_own_timer(self, handler, mock_state_manager):
        """The hold timer calls exit() through handle_force_start: it must finish the start."""
        mock_state_manager.connected_controllers = {"s1", "s2"}
        mock_state_manager.ready_controllers = {"s1", "s2"}
        handler._publish_event = AsyncMock()
        with patch.object(handler, "_force_start_threshold", return_value=0.01):
            await handler._start_trigger_hold_tracking("test_serial")
            await handler._trigger_hold_task

        handler.callbacks.start_game.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="A4")
    async def test_refused_force_start_restores_menu_state(self, handler, mock_state_manager):
        """B3: a refused start (live lifecycle) must not leave the menu in GAME_STARTING."""
        from proto import menu_pb2

        mock_state_manager.connected_controllers = {"s1", "s2"}
        mock_state_manager.ready_controllers = {"s1", "s2"}
        handler._publish_event = AsyncMock()
        handler.exit = AsyncMock()
        # Captured as RUNNING, then still GAME_STARTING when the refusal comes back.
        handler.callbacks.get_menu_state = MagicMock(
            side_effect=[menu_pb2.MenuState.RUNNING, menu_pb2.MenuState.GAME_STARTING]
        )
        handler.callbacks.start_game = AsyncMock(return_value=False)

        with patch("services.menu.handlers.admin.asyncio.sleep", new_callable=AsyncMock):
            await handler.handle_force_start("test_serial")

        states = [c.args[0] for c in handler.callbacks.set_menu_state.call_args_list]
        assert states[-1] == menu_pb2.MenuState.RUNNING

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="A4")
    async def test_refusal_does_not_overwrite_a_newer_menu_state(self, handler, mock_state_manager):
        """The state moved on during the awaits (not GAME_STARTING any more): leave it alone."""
        from proto import menu_pb2

        mock_state_manager.connected_controllers = {"s1", "s2"}
        mock_state_manager.ready_controllers = {"s1", "s2"}
        handler._publish_event = AsyncMock()
        handler.exit = AsyncMock()
        handler.callbacks.get_menu_state = MagicMock(
            side_effect=[menu_pb2.MenuState.RUNNING, menu_pb2.MenuState.STOPPED]
        )
        handler.callbacks.start_game = AsyncMock(return_value=False)

        with patch("services.menu.handlers.admin.asyncio.sleep", new_callable=AsyncMock):
            await handler.handle_force_start("test_serial")

        states = [c.args[0] for c in handler.callbacks.set_menu_state.call_args_list]
        assert states == [menu_pb2.MenuState.GAME_STARTING]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="A4")
    async def test_start_game_raising_restores_menu_state(self, handler, mock_state_manager):
        from proto import menu_pb2

        mock_state_manager.connected_controllers = {"s1", "s2"}
        mock_state_manager.ready_controllers = {"s1", "s2"}
        handler._publish_event = AsyncMock()
        handler.exit = AsyncMock()
        handler.callbacks.get_menu_state = MagicMock(
            side_effect=[menu_pb2.MenuState.RUNNING, menu_pb2.MenuState.GAME_STARTING]
        )
        handler.callbacks.start_game = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("services.menu.handlers.admin.asyncio.sleep", new_callable=AsyncMock):
            await handler.handle_force_start("test_serial")

        states = [c.args[0] for c in handler.callbacks.set_menu_state.call_args_list]
        assert states[-1] == menu_pb2.MenuState.RUNNING

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="main", steps=[5])
    async def test_exit_from_another_path_does_not_kill_a_force_start_past_its_hold(self, handler, mock_state_manager):
        """An idle-timeout exit() landing while the start is in flight must not cancel the hold timer."""
        mock_state_manager.connected_controllers = {"s1", "s2"}
        mock_state_manager.ready_controllers = {"s1", "s2"}
        handler._publish_event = AsyncMock()
        in_start = asyncio.Event()
        release = asyncio.Event()

        async def slow_roster(*_a, **_k):
            in_start.set()
            await release.wait()
            return ["s1", "s2"]

        handler.settings_ops.force_start_roster = slow_roster
        with patch.object(handler, "_force_start_threshold", return_value=0.01):
            await handler._start_trigger_hold_tracking("test_serial")
        timer = handler._trigger_hold_task
        await in_start.wait()

        await handler.exit()  # e.g. the idle timeout, a different task
        release.set()
        await timer

        handler.callbacks.start_game.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="main", steps=[5])
    async def test_trigger_release_after_hold_does_not_abort_force_start(self, handler, mock_state_manager):
        """Players release right after the 3 s hold: the in-flight start must still finish (#2377)."""
        mock_state_manager.connected_controllers = {"s1", "s2"}
        mock_state_manager.ready_controllers = {"s1", "s2"}
        handler._publish_event = AsyncMock()
        in_start = asyncio.Event()
        release = asyncio.Event()

        async def slow_roster(*_a, **_k):
            in_start.set()
            await release.wait()
            return ["s1", "s2"]

        handler.settings_ops.force_start_roster = slow_roster
        with patch.object(handler, "_force_start_threshold", return_value=0.01):
            await handler._start_trigger_hold_tracking("test_serial")
        timer = handler._trigger_hold_task
        await in_start.wait()

        await handler.handle_trigger_release("test_serial")
        release.set()
        await timer

        handler.callbacks.start_game.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-014", flow="A1")
    async def test_trigger_release_before_threshold_cancels_hold(self, handler):
        """A release before the hold elapses still cancels the pending force start."""
        handler.handle_force_start = AsyncMock()
        with patch.object(handler, "_force_start_threshold", return_value=0.05):
            await handler._start_trigger_hold_tracking("test_serial")
        timer = handler._trigger_hold_task

        await handler.handle_trigger_release("test_serial")
        await asyncio.sleep(0.1)

        assert timer.done()
        handler.handle_force_start.assert_not_awaited()
