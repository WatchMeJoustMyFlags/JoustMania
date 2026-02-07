"""Unit tests for AdminModeHandler game settings."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lib.colors import Colors
from services.menu.handlers.admin import AdminModeHandler


@pytest.fixture
def mock_tracer():
    """Create mock tracer."""
    tracer = MagicMock()
    tracer.start_span = MagicMock(return_value=MagicMock())
    tracer.start_as_current_span = MagicMock(return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()))
    return tracer


@pytest.fixture
def mock_callbacks():
    """Create mock callbacks."""
    callbacks = MagicMock()
    callbacks.get_game_options = MagicMock(return_value=["JoustFFA", "JoustTeams"])
    return callbacks


@pytest.fixture
def mock_metrics():
    """Create mock metrics."""
    metrics = MagicMock()
    metrics.button_presses_total = MagicMock()
    metrics.button_presses_total.labels = MagicMock(return_value=MagicMock())
    return metrics


@pytest.fixture
def mock_state_manager():
    """Create mock StateManager with FlagConfigWriter mocks."""
    manager = MagicMock()

    # FlagConfigWriter mocks for game_settings
    game_writer = MagicMock()
    game_writer.get_current_variant = MagicMock(return_value="medium")
    game_writer.cycle_variant = MagicMock(return_value="fast")
    game_writer.get_variant_names = MagicMock(return_value=["ultra_slow", "slow", "medium", "fast", "ultra_fast"])
    manager.game_settings_writer = game_writer

    # FlagConfigWriter mocks for user_preferences
    user_writer = MagicMock()
    user_writer.get_current_variant = MagicMock(return_value="on")
    user_writer.cycle_variant = MagicMock(return_value="off")
    manager.user_prefs_writer = user_writer

    manager.led = MagicMock()
    manager.led.send_game_effect = AsyncMock(return_value=True)
    manager.led.send_base_color = AsyncMock(return_value=True)
    manager.current_game_mode = MagicMock()
    manager.current_game_mode.name = "JoustFFA"
    manager.audio = MagicMock()
    manager.audio.play_voice = AsyncMock()
    manager.audio.play_sound = AsyncMock()
    manager.audio.play_game_mode_voice = AsyncMock()
    manager.publish_event = AsyncMock()
    return manager


@pytest.fixture
def handler(mock_tracer, mock_callbacks, mock_metrics, mock_state_manager):
    """Create AdminModeHandler instance with mocks."""
    handler = AdminModeHandler(
        controller_channel=MagicMock(),
        tracer=mock_tracer,
        callbacks=mock_callbacks,
        metrics=mock_metrics,
    )
    handler.set_state_manager(mock_state_manager)
    handler.active = True
    handler.controller_serial = "test_serial"
    return handler


class TestAdminOptionNavigation:
    """Tests for admin option navigation."""

    def test_option_names_defined(self, handler):
        """All expected options should be defined."""
        expected = [
            "sensitivity",
            "num_teams",
            "random_assignment",
            "nonstop_time_limit",
            "invincibility",
            "fight_club_min_rounds",
            "werewolf_reveal_time",
            "force_all_start",
        ]
        assert handler.option_names == expected

    def test_option_colors_match_names(self, handler):
        """Each option should have a corresponding color."""
        assert len(handler.option_colors) == len(handler.option_names)

    def test_option_colors_are_colors_enum(self, handler):
        """Option colors should be Colors enum values."""
        for color in handler.option_colors:
            assert isinstance(color, Colors)

    @pytest.mark.asyncio
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


class TestIncreaseValueSensitivity:
    """Tests for sensitivity increase via FlagConfigWriter."""

    @pytest.mark.asyncio
    async def test_sensitivity_cycles_forward(self, handler, mock_state_manager):
        """Sensitivity increase should call cycle_variant with forward=True."""
        handler.current_option = 0  # sensitivity

        await handler.handle_increase_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("sensitivity", forward=True)

    @pytest.mark.asyncio
    async def test_sensitivity_wraps_via_writer(self, handler, mock_state_manager):
        """Sensitivity wrapping is handled by FlagConfigWriter.cycle_variant."""
        handler.current_option = 0
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "ultra_slow"

        await handler.handle_increase_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("sensitivity", forward=True)


class TestIncreaseValueNumTeams:
    """Tests for num_teams increase via FlagConfigWriter."""

    @pytest.mark.asyncio
    async def test_num_teams_cycles_forward(self, handler, mock_state_manager):
        """num_teams increase should call cycle_variant with forward=True."""
        handler.current_option = 1  # num_teams

        await handler.handle_increase_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("num_teams", forward=True)


class TestIncreaseValueBoolean:
    """Tests for boolean settings increase (toggle) via FlagConfigWriter."""

    @pytest.mark.asyncio
    async def test_random_assignment_cycles_forward(self, handler, mock_state_manager):
        """random_assignment toggle should call cycle_variant with forward=True."""
        handler.current_option = 2  # random_assignment

        await handler.handle_increase_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("random_assignment", forward=True)

    @pytest.mark.asyncio
    async def test_force_all_start_cycles_forward(self, handler, mock_state_manager):
        """force_all_start toggle should call cycle_variant with forward=True."""
        handler.current_option = 7  # force_all_start

        await handler.handle_increase_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("force_all_start", forward=True)


class TestIncreaseValueNonstopTimeLimit:
    """Tests for nonstop_time_limit increase via FlagConfigWriter."""

    @pytest.mark.asyncio
    async def test_nonstop_time_limit_cycles_forward(self, handler, mock_state_manager):
        """nonstop_time_limit should call cycle_variant with forward=True."""
        handler.current_option = 3  # nonstop_time_limit

        await handler.handle_increase_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with(
            "nonstop_time_limit", forward=True
        )


class TestIncreaseValueInvincibility:
    """Tests for invincibility increase via FlagConfigWriter."""

    @pytest.mark.asyncio
    async def test_invincibility_cycles_forward(self, handler, mock_state_manager):
        """invincibility should call cycle_variant with forward=True."""
        handler.current_option = 4  # invincibility

        await handler.handle_increase_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("invincibility", forward=True)


class TestIncreaseValueFightClubMinRounds:
    """Tests for fight_club_min_rounds increase via FlagConfigWriter."""

    @pytest.mark.asyncio
    async def test_fight_club_min_rounds_cycles_forward(self, handler, mock_state_manager):
        """fight_club_min_rounds should call cycle_variant with forward=True."""
        handler.current_option = 5  # fight_club_min_rounds

        await handler.handle_increase_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with(
            "fight_club_min_rounds", forward=True
        )


class TestIncreaseValueWerewolfRevealTime:
    """Tests for werewolf_reveal_time increase via FlagConfigWriter."""

    @pytest.mark.asyncio
    async def test_werewolf_reveal_time_cycles_forward(self, handler, mock_state_manager):
        """werewolf_reveal_time should call cycle_variant with forward=True."""
        handler.current_option = 6  # werewolf_reveal_time

        await handler.handle_increase_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with(
            "werewolf_reveal_time", forward=True
        )


class TestDecreaseValueSensitivity:
    """Tests for sensitivity decrease via FlagConfigWriter."""

    @pytest.mark.asyncio
    async def test_sensitivity_cycles_backward(self, handler, mock_state_manager):
        """Sensitivity decrease should call cycle_variant with forward=False."""
        handler.current_option = 0  # sensitivity

        await handler.handle_decrease_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("sensitivity", forward=False)


class TestDecreaseValueNumTeams:
    """Tests for num_teams decrease via FlagConfigWriter."""

    @pytest.mark.asyncio
    async def test_num_teams_cycles_backward(self, handler, mock_state_manager):
        """num_teams decrease should call cycle_variant with forward=False."""
        handler.current_option = 1  # num_teams

        await handler.handle_decrease_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("num_teams", forward=False)


class TestDecreaseValueInvincibility:
    """Tests for invincibility decrease via FlagConfigWriter."""

    @pytest.mark.asyncio
    async def test_invincibility_cycles_backward(self, handler, mock_state_manager):
        """invincibility decrease should call cycle_variant with forward=False."""
        handler.current_option = 4  # invincibility

        await handler.handle_decrease_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("invincibility", forward=False)


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
    async def test_boolean_feedback_green_for_true(self, handler, mock_state_manager):
        """Boolean True should show green feedback."""
        await handler._show_value_feedback("test_serial", "force_all_start", True)

        call_kwargs = mock_state_manager.led.send_game_effect.call_args[1]
        assert call_kwargs["color"] == Colors.Green.value

    @pytest.mark.asyncio
    async def test_boolean_feedback_red_for_false(self, handler, mock_state_manager):
        """Boolean False should show red feedback."""
        await handler._show_value_feedback("test_serial", "force_all_start", False)

        call_kwargs = mock_state_manager.led.send_game_effect.call_args[1]
        assert call_kwargs["color"] == Colors.Red.value


class TestStateManagerIntegration:
    """Tests for state_manager.game_settings_writer integration."""

    @pytest.mark.asyncio
    async def test_uses_flag_config_writer_not_settings_service(self, handler, mock_state_manager):
        """Handler should use FlagConfigWriter, not Settings gRPC service."""
        handler.current_option = 0  # sensitivity

        # This should NOT make any gRPC calls - only calls FlagConfigWriter
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
    """Tests for handle_sensitivity using FlagConfigWriter."""

    @pytest.mark.asyncio
    async def test_handle_sensitivity_uses_flag_config_writer(self, handler, mock_state_manager):
        """handle_sensitivity should use FlagConfigWriter.cycle_variant."""
        mock_state_manager.game_settings_writer.get_current_variant.return_value = "medium"
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "fast"

        await handler.handle_sensitivity("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("sensitivity", forward=True)

    @pytest.mark.asyncio
    async def test_handle_sensitivity_wrapping_handled_by_writer(self, handler, mock_state_manager):
        """Sensitivity wrapping is delegated to FlagConfigWriter.cycle_variant."""
        mock_state_manager.game_settings_writer.get_current_variant.return_value = "ultra_fast"
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "ultra_slow"

        await handler.handle_sensitivity("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("sensitivity", forward=True)


class TestHandleForceStartFlagd:
    """Tests for handle_force_start using flagd."""

    @pytest.mark.asyncio
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
