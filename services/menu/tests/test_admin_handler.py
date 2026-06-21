"""Unit tests for AdminModeHandler game settings."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lib.colors import Colors
from lib.controller_constants import ButtonTrackingKey
from lib.types import Sound
from services.menu.handlers.admin import AdminModeHandler
from services.menu.handlers.base import ControllerState


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
    callbacks.select_game_mode = AsyncMock()
    callbacks.start_game = AsyncMock()
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

    # Effective-value resolution (#818): default to file value passthrough so
    # existing tests see today's behavior unless they override the client.
    game_writer.get_variant_value = MagicMock(return_value=2)
    game_writer.match_variant_for_value = MagicMock(return_value="medium")

    # FlagConfigWriter mocks for user_preferences
    user_writer = MagicMock()
    user_writer.get_current_variant = MagicMock(return_value="on")
    user_writer.cycle_variant = MagicMock(return_value="off")
    manager.user_prefs_writer = user_writer

    # FlagConfigWriter + client mocks for agent policy (#819 kill-switch).
    agent_writer = MagicMock()
    agent_writer.get_current_variant = MagicMock(return_value="ambient")
    agent_writer.update_default_variant = MagicMock(return_value=True)
    agent_writer.get_variant_value = MagicMock(return_value=[])
    agent_writer.match_variant_for_value = MagicMock(return_value="ambient")
    manager.agent_settings_writer = agent_writer

    # OpenFeature client mocks (#818 effective display / #819 level read).
    # By default the game client resolves to the file value (medium) so the
    # display matches the file; tests override to simulate agent overrides.
    game_client = MagicMock()
    game_client.get_string_value = MagicMock(return_value="medium")
    game_client.get_integer_value = MagicMock(return_value=2)
    game_client.get_object_value = MagicMock(return_value=["play_audio_cue"])
    manager.game_settings_client = game_client

    agent_client = MagicMock()
    agent_client.get_object_value = MagicMock(return_value=["play_audio_cue"])
    manager.agent_settings_client = agent_client

    # Interventions client (#818): default -1 (no active agent override).
    interventions_client = MagicMock()
    interventions_client.get_integer_value = MagicMock(return_value=-1)
    manager.interventions_client = interventions_client

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
    """Create AdminModeHandler instance with mocks (pre-activated)."""
    handler = AdminModeHandler(
        controller_channel=MagicMock(),
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
        controller_channel=MagicMock(),
        tracer=mock_tracer,
        callbacks=mock_callbacks,
        metrics=mock_metrics,
    )
    handler.set_state_manager(mock_state_manager)
    return handler


class TestAdminOptionNavigation:
    """Tests for admin option navigation."""

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

    @pytest.mark.asyncio
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
    async def test_cycle_option_index_always_in_bounds(self, handler):
        """current_option must stay a valid index across many cycles."""
        handler.current_option = 0
        for _ in range(10):
            await handler.handle_cycle_option("test_serial")
            assert 0 <= handler.current_option < len(handler.option_names)


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
    async def test_force_all_start_cycles_forward(self, handler, mock_state_manager):
        """force_all_start toggle should call cycle_variant with forward=True."""
        handler.current_option = 2  # force_all_start (index 2 in reduced surface)

        await handler.handle_increase_value("test_serial")

        mock_state_manager.game_settings_writer.cycle_variant.assert_called_once_with("force_all_start", forward=True)


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


class TestDecreaseValueForceAllStart:
    """Tests for force_all_start decrease via FlagConfigWriter."""

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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
    async def test_enter_records_metric(self, inactive_handler, mock_metrics):
        """enter() should increment button_presses_total for admin_combo."""
        await inactive_handler.enter("s1")
        mock_metrics.button_presses_total.labels.assert_called_with(button="admin_combo", action="hold")
        mock_metrics.button_presses_total.labels().inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_enter_sends_admin_enter_effect(self, inactive_handler, mock_state_manager):
        """enter() should send GAME_EFFECT_ADMIN_ENTER."""
        from proto import controller_manager_pb2

        await inactive_handler.enter("s1")
        call_args = mock_state_manager.led.send_game_effect.call_args
        assert call_args[0][0] == "s1"
        assert call_args[0][1] == controller_manager_pb2.GAME_EFFECT_ADMIN_ENTER

    @pytest.mark.asyncio
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
    async def test_dispatch_move_to_cycle_option(self, handler):
        """MOVE button should dispatch to handle_cycle_option."""
        handler.handle_cycle_option = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.MOVE)
        handler.handle_cycle_option.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    async def test_dispatch_trigger_to_hold_tracking(self, handler):
        """TRIGGER button should dispatch to _start_trigger_hold_tracking."""
        handler._start_trigger_hold_tracking = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.TRIGGER)
        handler._start_trigger_hold_tracking.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    async def test_dispatch_select_to_increase(self, handler):
        """SELECT button should dispatch to handle_increase_value."""
        handler.handle_increase_value = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.SELECT)
        handler.handle_increase_value.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    async def test_dispatch_start_to_decrease(self, handler):
        """START button should dispatch to handle_decrease_value."""
        handler.handle_decrease_value = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.START)
        handler.handle_decrease_value.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    async def test_dispatch_cross_to_game_mode(self, handler):
        """CROSS button should dispatch to handle_game_mode."""
        handler.handle_game_mode = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.CROSS)
        handler.handle_game_mode.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    async def test_dispatch_circle_to_sensitivity(self, handler):
        """CIRCLE button should dispatch to handle_sensitivity."""
        handler.handle_sensitivity = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.CIRCLE)
        handler.handle_sensitivity.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    async def test_dispatch_triangle_to_battery(self, handler):
        """TRIANGLE button should dispatch to handle_battery."""
        handler.handle_battery = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.TRIANGLE)
        handler.handle_battery.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    async def test_dispatch_square_to_instructions(self, handler):
        """SQUARE button should dispatch to handle_instructions."""
        handler.handle_instructions = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.SQUARE)
        handler.handle_instructions.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    async def test_dispatch_ps_to_exit(self, handler):
        """PS button should dispatch to _exit_to_connected."""
        handler._exit_to_connected = AsyncMock()
        await handler.handle_button_event("test_serial", ButtonTrackingKey.PS)
        handler._exit_to_connected.assert_called_once_with("test_serial")

    @pytest.mark.asyncio
    async def test_timeout_exits_after_60_seconds(self, handler):
        """Should auto-exit if entry_time was more than 60 seconds ago."""
        handler.entry_time = time.time() - 61
        handler._exit_to_connected = AsyncMock()

        await handler.handle_button_event("test_serial", ButtonTrackingKey.CROSS)

        handler._exit_to_connected.assert_called_once_with("test_serial")

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


class TestResetOnDisconnect:
    """Tests for admin mode reset on controller disconnect."""

    def test_clears_state_for_matching_serial(self, handler):
        """reset_on_disconnect should clear state when serial matches."""
        handler.reset_on_disconnect("test_serial")

        assert handler.active is False
        assert handler.controller_serial is None
        assert handler.entry_time == 0

    def test_ignores_different_serial(self, handler):
        """reset_on_disconnect should ignore non-admin controller."""
        handler.reset_on_disconnect("other_serial")

        assert handler.active is True
        assert handler.controller_serial == "test_serial"

    def test_ignores_when_inactive(self, inactive_handler):
        """reset_on_disconnect should do nothing when not active."""
        inactive_handler.reset_on_disconnect("s1")
        assert inactive_handler.active is False

    def test_cancels_timeout_task(self, handler):
        """reset_on_disconnect should cancel timeout task."""
        mock_task = MagicMock()
        mock_task.done.return_value = False
        handler._timeout_task = mock_task

        handler.reset_on_disconnect("test_serial")

        mock_task.cancel.assert_called_once()

    def test_ends_session_span(self, handler):
        """reset_on_disconnect should end the session span."""
        span = MagicMock()
        handler.session_span = span

        handler.reset_on_disconnect("test_serial")

        span.set_attribute.assert_called_with("disconnect", True)
        span.end.assert_called_once()
        assert handler.session_span is None


# ============================================================================
# Tier 2 — Pure Logic
# ============================================================================


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

    def test_random_assignment_on(self):
        assert AdminModeHandler._variant_to_value("random_assignment", "on") is True

    def test_random_assignment_off(self):
        assert AdminModeHandler._variant_to_value("random_assignment", "off") is False

    def test_nonstop_time_limit_unlimited(self):
        assert AdminModeHandler._variant_to_value("nonstop.time_limit_seconds", "unlimited") == 0

    def test_nonstop_time_limit_five_min(self):
        assert AdminModeHandler._variant_to_value("nonstop.time_limit_seconds", "five_min") == 300

    def test_invincibility_2s(self):
        assert AdminModeHandler._variant_to_value("invincibility_seconds", "2s") == 2.0

    def test_invincibility_8s(self):
        assert AdminModeHandler._variant_to_value("invincibility_seconds", "8s") == 8.0

    def test_fight_club_min_rounds_five(self):
        assert AdminModeHandler._variant_to_value("fight_club.min_rounds", "five") == 5

    def test_fight_club_min_rounds_twenty(self):
        assert AdminModeHandler._variant_to_value("fight_club.min_rounds", "twenty") == 20

    def test_werewolf_reveal_time_20s(self):
        assert AdminModeHandler._variant_to_value("werewolf.reveal_time_seconds", "20s") == 20.0

    def test_werewolf_reveal_time_60s(self):
        assert AdminModeHandler._variant_to_value("werewolf.reveal_time_seconds", "60s") == 60.0

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

    def test_combo_from_controller(self, handler):
        """check_combo_from_controller with all pressed should return True."""
        controller = MagicMock()
        controller.cross_pressed = True
        controller.circle_pressed = True
        controller.square_pressed = True
        controller.triangle_pressed = True
        assert handler.check_combo_from_controller(controller) is True

    def test_combo_from_controller_missing(self, handler):
        """check_combo_from_controller with one missing should return False."""
        controller = MagicMock()
        controller.cross_pressed = True
        controller.circle_pressed = False
        controller.square_pressed = True
        controller.triangle_pressed = True
        assert handler.check_combo_from_controller(controller) is False


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
    async def test_wraps_to_first_mode(self, handler, mock_state_manager):
        """Should wrap to first mode when at end."""
        mock_state_manager.current_game_mode.name = "JoustTeams"
        handler.callbacks.get_game_options.return_value = ["JoustFFA", "JoustTeams"]

        await handler.handle_game_mode("test_serial")

        call_args = mock_state_manager.publish_event.call_args[0]
        assert call_args[1]["game_name"] == "JoustFFA"

    @pytest.mark.asyncio
    async def test_sends_pulse_and_plays_voice(self, handler, mock_state_manager):
        """Should send color pulse and play game mode voice."""
        from proto import controller_manager_pb2

        await handler.handle_game_mode("test_serial")

        mock_state_manager.led.send_game_effect.assert_called_once()
        call_args = mock_state_manager.led.send_game_effect.call_args
        assert call_args[0][1] == controller_manager_pb2.GAME_EFFECT_PULSE
        mock_state_manager.audio.play_game_mode_voice.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_options_returns_early(self, handler, mock_state_manager):
        """Should return early if no game options available."""
        handler.callbacks.get_game_options.return_value = []

        await handler.handle_game_mode("test_serial")

        mock_state_manager.publish_event.assert_not_called()
        handler.callbacks.select_game_mode.assert_not_called()

    @pytest.mark.asyncio
    async def test_applies_selection_via_callback(self, handler, mock_state_manager):
        """Cross cycling must actually apply the new selection through the menu
        (#1030) — not just publish a fire-and-forget event."""
        mock_state_manager.current_game_mode.name = "JoustFFA"
        handler.callbacks.get_game_options.return_value = ["JoustFFA", "JoustTeams", "Werewolf"]

        await handler.handle_game_mode("test_serial")

        handler.callbacks.select_game_mode.assert_awaited_once_with("JoustTeams")


class TestHandleGameModeChange:
    """Tests for handle_game_mode_change with direction."""

    @pytest.mark.asyncio
    async def test_forward_sends_green_pulse(self, handler, mock_state_manager):
        """Forward mode change should send green pulse."""
        await handler.handle_game_mode_change("test_serial", forward=True)

        call_kwargs = mock_state_manager.led.send_game_effect.call_args[1]
        assert call_kwargs["color"] == (0, 255, 0)

    @pytest.mark.asyncio
    async def test_backward_sends_blue_pulse(self, handler, mock_state_manager):
        """Backward mode change should send blue pulse."""
        await handler.handle_game_mode_change("test_serial", forward=False)

        call_kwargs = mock_state_manager.led.send_game_effect.call_args[1]
        assert call_kwargs["color"] == (0, 0, 255)

    @pytest.mark.asyncio
    async def test_empty_options_returns_early(self, handler, mock_state_manager):
        """Should return early if no game options."""
        handler.callbacks.get_game_options.return_value = []

        await handler.handle_game_mode_change("test_serial")

        mock_state_manager.publish_event.assert_not_called()
        handler.callbacks.select_game_mode.assert_not_called()

    @pytest.mark.asyncio
    async def test_forward_applies_selection_via_callback(self, handler, mock_state_manager):
        """Forward cycling must actually apply the new selection through the menu
        (same path as handle_game_mode) — not just publish a fire-and-forget
        event (#1030). Guards against a future re-wiring reintroducing the bug."""
        mock_state_manager.current_game_mode.name = "JoustFFA"
        handler.callbacks.get_game_options.return_value = ["JoustFFA", "JoustTeams", "Werewolf"]

        await handler.handle_game_mode_change("test_serial", forward=True)

        handler.callbacks.select_game_mode.assert_awaited_once_with("JoustTeams")

    @pytest.mark.asyncio
    async def test_backward_applies_selection_via_callback(self, handler, mock_state_manager):
        """Backward cycling must also apply the selection through the menu."""
        mock_state_manager.current_game_mode.name = "JoustFFA"
        handler.callbacks.get_game_options.return_value = ["JoustFFA", "JoustTeams", "Werewolf"]

        await handler.handle_game_mode_change("test_serial", forward=False)

        handler.callbacks.select_game_mode.assert_awaited_once_with("Werewolf")


class TestHandleBattery:
    """Tests for battery display (triangle button)."""

    @pytest.mark.asyncio
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
    async def test_cycles_variant(self, handler, mock_state_manager):
        """Should cycle game_instructions variant."""
        await handler.handle_instructions("test_serial")

        mock_state_manager.user_prefs_writer.cycle_variant.assert_called_once_with("game_instructions", forward=True)

    @pytest.mark.asyncio
    async def test_enabled_shows_green_and_on_voice(self, handler, mock_state_manager):
        """When toggled to 'on', should show green and play on voice."""
        mock_state_manager.user_prefs_writer.cycle_variant.return_value = "on"

        await handler.handle_instructions("test_serial")

        call_kwargs = mock_state_manager.led.send_game_effect.call_args[1]
        assert call_kwargs["color"] == (0, 255, 0)
        mock_state_manager.audio.play_voice.assert_called_once_with(Sound.MENU_VOX_INSTRUCTIONS_ON)

    @pytest.mark.asyncio
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
    async def test_hold_starts_effect_and_creates_task(self, handler, mock_state_manager):
        """Trigger press should start force start effect and create hold task."""
        from proto import controller_manager_pb2

        await handler._start_trigger_hold_tracking("test_serial")

        assert handler._trigger_press_time is not None
        assert handler._trigger_hold_task is not None
        call_args = mock_state_manager.led.send_game_effect.call_args
        assert call_args[0][0] == "test_serial"
        assert call_args[0][1] == controller_manager_pb2.GAME_EFFECT_FORCE_START_CHARGE
        # Clean up
        handler._trigger_hold_task.cancel()

    @pytest.mark.asyncio
    async def test_release_cancels_task_and_restores_white(self, handler, mock_state_manager):
        """Trigger release should cancel hold task and restore white."""
        # Start the hold first
        await handler._start_trigger_hold_tracking("test_serial")
        mock_state_manager.led.send_game_effect.reset_mock()

        # Release
        await handler.handle_trigger_release("test_serial")

        assert handler._trigger_press_time is None
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
    async def test_num_teams_2_is_green(self, handler, mock_state_manager):
        """num_teams=2 should produce green (0, 255, 0)."""
        await handler._show_value_feedback("test_serial", "num_teams", 2)
        call_kwargs = mock_state_manager.led.send_game_effect.call_args[1]
        assert call_kwargs["color"] == (0, 255, 0)

    @pytest.mark.asyncio
    async def test_num_teams_6_is_red(self, handler, mock_state_manager):
        """num_teams=6 should produce red (255, 0, 0)."""
        await handler._show_value_feedback("test_serial", "num_teams", 6)
        call_kwargs = mock_state_manager.led.send_game_effect.call_args[1]
        assert call_kwargs["color"] == (255, 0, 0)

    @pytest.mark.asyncio
    async def test_nonstop_unlimited_dim_purple(self, handler, mock_state_manager):
        """nonstop_time_limit=0 (unlimited) should produce dim purple (50, 0, 50)."""
        await handler._show_value_feedback("test_serial", "nonstop.time_limit_seconds", 0)
        call_kwargs = mock_state_manager.led.send_game_effect.call_args[1]
        assert call_kwargs["color"] == (50, 0, 50)

    @pytest.mark.asyncio
    async def test_nonstop_300_bright_purple(self, handler, mock_state_manager):
        """nonstop_time_limit=300 should produce bright purple (255, 0, 255)."""
        await handler._show_value_feedback("test_serial", "nonstop.time_limit_seconds", 300)
        call_kwargs = mock_state_manager.led.send_game_effect.call_args[1]
        assert call_kwargs["color"] == (255, 0, 255)

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
    async def test_force_all_start_true_plays_true_voice(self, handler, mock_state_manager):
        """force_all_start=True should play TRUE voice (bug fix proof).

        Before fix: value == "true" never matched bool True.
        After fix: value is True correctly matches.
        """
        await handler._play_value_voice("force_all_start", True)
        mock_state_manager.audio.play_voice.assert_called_once_with(Sound.MENU_VOX_ADMINOP_TRUE)

    @pytest.mark.asyncio
    async def test_force_all_start_false_plays_false_voice(self, handler, mock_state_manager):
        """force_all_start=False should play FALSE voice."""
        await handler._play_value_voice("force_all_start", False)
        mock_state_manager.audio.play_voice.assert_called_once_with(Sound.MENU_VOX_ADMINOP_FALSE)

    @pytest.mark.asyncio
    async def test_other_option_no_voice(self, handler, mock_state_manager):
        """Non num_teams/force_all_start options should not play voice."""
        await handler._play_value_voice("sensitivity", 2)
        mock_state_manager.audio.play_voice.assert_not_called()


class TestControllerHandlerProtocol:
    """Tests for the ControllerHandler protocol methods."""

    @pytest.mark.asyncio
    async def test_handle_button_delegates(self, handler):
        """handle_button should delegate to handle_button_event."""
        handler.handle_button_event = AsyncMock()
        await handler.handle_button("s1", "cross")
        handler.handle_button_event.assert_called_once_with("s1", "cross")

    @pytest.mark.asyncio
    async def test_on_enter_delegates(self, inactive_handler):
        """on_enter should delegate to enter."""
        inactive_handler.enter = AsyncMock()
        await inactive_handler.on_enter("s1")
        inactive_handler.enter.assert_called_once_with("s1")

    @pytest.mark.asyncio
    async def test_on_exit_delegates(self, handler):
        """on_exit should delegate to exit."""
        handler.exit = AsyncMock()
        await handler.on_exit("s1")
        handler.exit.assert_called_once()

    def test_set_state_manager(self, inactive_handler, mock_state_manager):
        """set_state_manager should store the manager."""
        inactive_handler._state_manager = None
        inactive_handler.set_state_manager(mock_state_manager)
        assert inactive_handler._state_manager is mock_state_manager


class TestHelperProperties:
    """Tests for StateManager helper properties."""

    def test_connected_controllers_with_manager(self, handler, mock_state_manager):
        """Should return connected_controllers from state_manager."""
        mock_state_manager.connected_controllers = {"s1", "s2"}
        assert handler._connected_controllers == {"s1", "s2"}

    def test_connected_controllers_without_manager(self, handler):
        """Should return empty set without state_manager."""
        handler._state_manager = None
        assert handler._connected_controllers == set()

    def test_ready_controllers_with_manager(self, handler, mock_state_manager):
        """Should return ready_controllers from state_manager."""
        mock_state_manager.ready_controllers = {"s1"}
        assert handler._ready_controllers == {"s1"}

    def test_ready_controllers_without_manager(self, handler):
        """Should return empty set without state_manager."""
        handler._state_manager = None
        assert handler._ready_controllers == set()

    def test_current_game_mode_with_manager(self, handler, mock_state_manager):
        """Should return game mode name from state_manager."""
        assert handler._current_game_mode == "JoustFFA"

    def test_current_game_mode_without_manager(self, handler):
        """Should return 'JoustFFA' default without state_manager."""
        handler._state_manager = None
        assert handler._current_game_mode == "JoustFFA"


class TestAgentControlOption:
    """Tests for the on-device agent kill-switch admin option (#819)."""

    def test_agent_control_in_option_cycle(self, handler):
        """agent_control must be a controller-cycled option with a color."""
        from services.menu.handlers.admin import AGENT_CONTROL_OPTION

        assert AGENT_CONTROL_OPTION in handler.option_names
        idx = handler.option_names.index(AGENT_CONTROL_OPTION)
        assert isinstance(handler.option_colors[idx], Colors)

    @pytest.mark.asyncio
    async def test_increase_routes_to_agent_control(self, handler, mock_state_manager):
        """SELECT on the agent option escalates policy, not a game.json write."""
        from services.menu.handlers.admin import AGENT_CONTROL_OPTION

        handler.current_option = handler.option_names.index(AGENT_CONTROL_OPTION)
        mock_state_manager.agent_settings_writer.match_variant_for_value.return_value = "ambient"

        await handler.handle_increase_value("test_serial")

        # Wrote interventions_allowed in agent.json, escalating ambient -> standard.
        mock_state_manager.agent_settings_writer.update_default_variant.assert_called_once_with(
            "interventions_allowed", "standard"
        )
        # Did NOT touch the game settings writer.
        mock_state_manager.game_settings_writer.cycle_variant.assert_not_called()

    @pytest.mark.asyncio
    async def test_decrease_restricts_agent_policy(self, handler, mock_state_manager):
        """START on the agent option restricts policy one level toward none."""
        from services.menu.handlers.admin import AGENT_CONTROL_OPTION

        handler.current_option = handler.option_names.index(AGENT_CONTROL_OPTION)
        mock_state_manager.agent_settings_writer.match_variant_for_value.return_value = "standard"

        await handler.handle_decrease_value("test_serial")

        # standard -> ambient (one step less permissive).
        mock_state_manager.agent_settings_writer.update_default_variant.assert_called_once_with(
            "interventions_allowed", "ambient"
        )

    @pytest.mark.asyncio
    async def test_panic_off_wraps_from_none_backward(self, handler, mock_state_manager):
        """Restricting from 'none' wraps to the most-permissive 'full' (cycle)."""
        handler.current_option = handler.option_names.index("agent_control")
        mock_state_manager.agent_settings_writer.match_variant_for_value.return_value = "none"

        await handler.handle_decrease_value("test_serial")

        mock_state_manager.agent_settings_writer.update_default_variant.assert_called_once_with(
            "interventions_allowed", "full"
        )

    @pytest.mark.asyncio
    async def test_full_escalates_to_none_wrap(self, handler, mock_state_manager):
        """Escalating from 'full' wraps to 'none' (cycle through 4 levels)."""
        handler.current_option = handler.option_names.index("agent_control")
        mock_state_manager.agent_settings_writer.match_variant_for_value.return_value = "full"

        await handler.handle_increase_value("test_serial")

        mock_state_manager.agent_settings_writer.update_default_variant.assert_called_once_with(
            "interventions_allowed", "none"
        )

    @pytest.mark.asyncio
    async def test_agent_control_led_and_voice_feedback(self, handler, mock_state_manager):
        """Cycling agent policy emits an LED pulse and a voice line."""
        from proto import controller_manager_pb2

        handler.current_option = handler.option_names.index("agent_control")
        mock_state_manager.agent_settings_writer.match_variant_for_value.return_value = "ambient"

        await handler.handle_increase_value("test_serial")

        # LED pulse feedback was sent.
        mock_state_manager.led.send_game_effect.assert_called()
        effect = mock_state_manager.led.send_game_effect.call_args[0][1]
        assert effect == controller_manager_pb2.GAME_EFFECT_PULSE
        # Voice feedback was played.
        mock_state_manager.audio.play_voice.assert_called()

    @pytest.mark.asyncio
    async def test_write_failure_skips_feedback(self, handler, mock_state_manager):
        """A failed agent.json write must not emit feedback as if it succeeded."""
        handler.current_option = handler.option_names.index("agent_control")
        mock_state_manager.agent_settings_writer.update_default_variant.return_value = False

        await handler.handle_increase_value("test_serial")

        mock_state_manager.led.send_game_effect.assert_not_called()
        mock_state_manager.audio.play_voice.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_agent_writer_is_safe(self, handler, mock_state_manager):
        """Missing agent writer logs and no-ops rather than raising."""
        handler.current_option = handler.option_names.index("agent_control")
        mock_state_manager.agent_settings_writer = None

        # Should not raise.
        await handler.handle_increase_value("test_serial")

    def test_next_agent_level_cycle(self):
        """_next_agent_level cycles the 4 levels forward and backward."""
        assert AdminModeHandler._next_agent_level("none", forward=True) == "ambient"
        assert AdminModeHandler._next_agent_level("ambient", forward=True) == "standard"
        assert AdminModeHandler._next_agent_level("standard", forward=True) == "full"
        assert AdminModeHandler._next_agent_level("full", forward=True) == "none"
        assert AdminModeHandler._next_agent_level("ambient", forward=False) == "none"
        # Unknown current value: forward starts the ladder, backward = panic-off.
        assert AdminModeHandler._next_agent_level(None, forward=True) == "none"
        assert AdminModeHandler._next_agent_level("bogus", forward=False) == "none"


class TestEffectiveValueDisplay:
    """Tests for the truthful admin value display resolved through flagd (#818)."""

    def test_effective_variant_reflects_agent_override(self, handler, mock_state_manager):
        """When the agent overrode sensitivity, the display shows the LIVE value,
        not the stale on-disk defaultVariant."""
        # On disk: medium. flagd resolves the effective integer to 4 (ultra_fast).
        mock_state_manager.game_settings_writer.get_current_variant.return_value = "medium"
        mock_state_manager.game_settings_writer.get_variant_value.return_value = 2
        mock_state_manager.game_settings_client.get_integer_value.return_value = 4
        mock_state_manager.game_settings_writer.match_variant_for_value.return_value = "ultra_fast"

        result = handler._effective_variant("sensitivity")

        assert result == "ultra_fast"
        # Resolved through the live client, not just the file default.
        mock_state_manager.game_settings_client.get_integer_value.assert_called()

    def test_effective_variant_falls_back_to_file_without_client(self, handler, mock_state_manager):
        """No client wired -> degrade to the on-disk defaultVariant."""
        mock_state_manager.game_settings_client = None
        mock_state_manager.game_settings_writer.get_current_variant.return_value = "fast"

        assert handler._effective_variant("sensitivity") == "fast"

    def test_effective_variant_falls_back_on_no_match(self, handler, mock_state_manager):
        """Resolved value matching no known variant -> file default."""
        mock_state_manager.game_settings_writer.get_current_variant.return_value = "medium"
        mock_state_manager.game_settings_writer.get_variant_value.return_value = 2
        mock_state_manager.game_settings_client.get_integer_value.return_value = 99
        mock_state_manager.game_settings_writer.match_variant_for_value.return_value = None

        assert handler._effective_variant("sensitivity") == "medium"

    def test_effective_variant_falls_back_on_exception(self, handler, mock_state_manager):
        """Any resolution error -> file default (never blank)."""
        mock_state_manager.game_settings_writer.get_current_variant.return_value = "slow"
        mock_state_manager.game_settings_writer.get_variant_value.side_effect = RuntimeError("boom")

        assert handler._effective_variant("sensitivity") == "slow"

    def test_sensitivity_shows_active_agent_override(self, handler, mock_state_manager):
        """The headline #818 case: baseline medium, agent overrode to ultra_fast.

        The operator must see ultra_fast (the value the game is actually using),
        not the stale game.json baseline.
        """
        mock_state_manager.game_settings_writer.get_current_variant.return_value = "medium"
        # Agent's global_sensitivity_override resolves to 4 (ultra_fast).
        mock_state_manager.interventions_client.get_integer_value.return_value = 4

        assert handler._effective_variant("sensitivity") == "ultra_fast"

    def test_sensitivity_no_override_uses_baseline(self, handler, mock_state_manager):
        """override = -1 (none) -> fall through to the human baseline value."""
        mock_state_manager.game_settings_writer.get_current_variant.return_value = "medium"
        mock_state_manager.interventions_client.get_integer_value.return_value = -1
        mock_state_manager.game_settings_writer.get_variant_value.return_value = 2
        mock_state_manager.game_settings_client.get_integer_value.return_value = 2
        mock_state_manager.game_settings_writer.match_variant_for_value.return_value = "medium"

        assert handler._effective_variant("sensitivity") == "medium"


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
        # Active agent override -> _effective_variant would resolve ultra_fast.
        mock_state_manager.interventions_client.get_integer_value.return_value = 4

        await handler.handle_sensitivity("test_serial")

        _name, attrs = self._cycle_event(handler)
        assert attrs["old_variant"] == "medium"
        assert attrs["new_variant"] == "fast"
        # The effective-display path must NOT have been used to source old_variant.
        mock_state_manager.interventions_client.get_integer_value.assert_not_called()

    @pytest.mark.asyncio
    async def test_increase_value_old_variant_is_baseline_not_override(self, handler, mock_state_manager):
        """handle_increase_value cycle feedback is the baseline transition."""
        handler.current_option = handler.option_names.index("sensitivity")
        mock_state_manager.game_settings_writer.get_current_variant.return_value = "medium"
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "fast"
        mock_state_manager.interventions_client.get_integer_value.return_value = 4

        await handler.handle_increase_value("test_serial")

        _name, attrs = self._cycle_event(handler)
        assert attrs["old_variant"] == "medium"
        assert attrs["new_variant"] == "fast"
        mock_state_manager.interventions_client.get_integer_value.assert_not_called()

    @pytest.mark.asyncio
    async def test_decrease_value_old_variant_is_baseline_not_override(self, handler, mock_state_manager):
        """handle_decrease_value cycle feedback is the baseline transition."""
        handler.current_option = handler.option_names.index("sensitivity")
        mock_state_manager.game_settings_writer.get_current_variant.return_value = "medium"
        mock_state_manager.game_settings_writer.cycle_variant.return_value = "slow"
        mock_state_manager.interventions_client.get_integer_value.return_value = 4

        await handler.handle_decrease_value("test_serial")

        _name, attrs = self._cycle_event(handler)
        assert attrs["old_variant"] == "medium"
        assert attrs["new_variant"] == "slow"
        mock_state_manager.interventions_client.get_integer_value.assert_not_called()


class TestForceStartThresholdFlag:
    """The trigger-hold force-start threshold reads from the game.json
    ``force_all_start_seconds`` flag (#1215), migrated from the
    ADMIN_FORCE_START_SECONDS env var. Constructed under a patched
    ``read_float_flag`` so the __init__-time read resolves to the test value.
    """

    def _build(self, mock_tracer, mock_callbacks, mock_metrics):
        return AdminModeHandler(
            controller_channel=MagicMock(),
            tracer=mock_tracer,
            callbacks=mock_callbacks,
            metrics=mock_metrics,
        )

    def test_threshold_read_from_flag(self, mock_tracer, mock_callbacks, mock_metrics):
        with patch("services.menu.handlers.admin.read_float_flag", return_value=0.6) as mock_read:
            handler = self._build(mock_tracer, mock_callbacks, mock_metrics)
        assert handler._force_start_threshold == 0.6
        mock_read.assert_called_once_with("game", "force_all_start_seconds", 3.0)

    def test_threshold_fails_open_to_default(self, mock_tracer, mock_callbacks, mock_metrics):
        """When flagd is unreachable/undefined at startup (#1177), read_float_flag
        returns the supplied 3.0 default — the real-play hold duration."""
        with patch(
            "services.menu.handlers.admin.read_float_flag",
            side_effect=lambda _domain, _key, default, _game_id=None: default,
        ):
            handler = self._build(mock_tracer, mock_callbacks, mock_metrics)
        assert handler._force_start_threshold == 3.0
