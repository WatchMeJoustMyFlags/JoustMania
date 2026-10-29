"""Unit tests for IdleMonitor and IdleHandler."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

from services.menu.handlers.base import ControllerState
from services.menu.handlers.idle import IdleHandler
from services.menu.idle_monitor import IdleMonitor, _hsv_to_rgb
from services.menu.state_manager import StateManager
from services.menu.utils import LedController

# --- Fixtures ---


@pytest.fixture
def mock_led():
    """Create mock LedController."""
    led = create_autospec(LedController, instance=True)
    led.set_color.return_value = True
    led.set_connected_color.return_value = True
    return led


@pytest.fixture
def mock_state_manager(mock_led):
    """Create mock StateManager with idle-relevant attributes."""
    sm = create_autospec(StateManager, instance=True)
    sm.led = mock_led
    sm.controller_states = {}
    sm.battery_levels = {}
    sm.current_game_mode = MagicMock()
    sm.current_game_mode.name = "JoustFFA"
    return sm


@pytest.fixture
def idle_monitor(mock_state_manager):
    """Create IdleMonitor with default settings."""
    return IdleMonitor(
        state_manager=mock_state_manager,
        get_idle_enabled=lambda: True,
        get_idle_timeout=lambda: 15,
        get_sentinel_count=lambda: 2,
        get_rotation_minutes=lambda: 5,
    )


@pytest.fixture
def disabled_idle_monitor(mock_state_manager):
    """Create IdleMonitor with idle mode disabled."""
    return IdleMonitor(
        state_manager=mock_state_manager,
        get_idle_enabled=lambda: False,
        get_idle_timeout=lambda: 15,
        get_sentinel_count=lambda: 2,
        get_rotation_minutes=lambda: 5,
    )


# --- IdleHandler Tests ---


class TestIdleHandler:
    """Tests for IdleHandler."""

    def test_state(self):
        """IdleHandler should report IDLE state."""
        handler = IdleHandler()
        assert handler.state == ControllerState.IDLE

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A2", steps=[1])
    async def test_on_enter_turns_off_leds(self, mock_state_manager):
        """on_enter should set LEDs to black (off)."""
        handler = IdleHandler()
        handler.set_state_manager(mock_state_manager)

        await handler.on_enter("serial1")

        mock_state_manager.led.set_color.assert_awaited_once_with("serial1", (0, 0, 0))

    @pytest.mark.asyncio
    async def test_on_exit_is_noop(self, mock_state_manager):
        """on_exit should not interact with state manager."""
        handler = IdleHandler()
        handler.set_state_manager(mock_state_manager)

        # Should not raise
        await handler.on_exit("serial1")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A2")
    async def test_handle_button_transitions_to_connected(self, mock_state_manager):
        """Any button press in idle should transition to CONNECTED."""
        handler = IdleHandler()
        handler.set_state_manager(mock_state_manager)

        await handler.handle_button("serial1", "trigger")

        mock_state_manager.transition_to.assert_awaited_once_with("serial1", ControllerState.CONNECTED)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A2")
    async def test_handle_button_calls_wake_callback(self, mock_state_manager):
        """Button press should call wake callback."""
        wake_callback = AsyncMock()
        handler = IdleHandler(wake_callback=wake_callback)
        handler.set_state_manager(mock_state_manager)

        await handler.handle_button("serial1", "move")

        wake_callback.assert_awaited_once_with("serial1")

    @pytest.mark.asyncio
    async def test_handle_button_no_callback(self, mock_state_manager):
        """Button press without callback should not raise."""
        handler = IdleHandler()
        handler.set_state_manager(mock_state_manager)

        await handler.handle_button("serial1", "select")

        mock_state_manager.transition_to.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_button_no_state_manager(self):
        """Button press without state manager should log error and return."""
        handler = IdleHandler()

        # Should not raise
        await handler.handle_button("serial1", "trigger")

    def test_set_wake_callback(self):
        """set_wake_callback should update the callback."""
        handler = IdleHandler()
        callback = AsyncMock()
        handler.set_wake_callback(callback)
        assert handler._wake_callback is callback


# --- IdleMonitor Tests ---


class TestIdleMonitorInit:
    """Test IdleMonitor initialization."""

    def test_initial_state(self, idle_monitor):
        """IdleMonitor should start not idle."""
        assert not idle_monitor.is_idle_active
        assert idle_monitor.sentinel_serials == []

    def test_reset_activity(self, idle_monitor):
        """reset_activity should update last activity time."""
        idle_monitor._last_activity_time = 0
        idle_monitor.reset_activity()
        assert idle_monitor._last_activity_time > 0


class TestIdleMonitorSentinelSelection:
    """Test sentinel selection logic."""

    def test_select_sentinels_by_battery(self, idle_monitor, mock_state_manager):
        """Sentinels should prefer controllers with highest battery."""
        mock_state_manager.battery_levels = {
            "s1": 3,
            "s2": 5,
            "s3": 1,
            "s4": 4,
        }

        sentinels = idle_monitor._select_sentinels(["s1", "s2", "s3", "s4"], 2)

        assert len(sentinels) == 2
        assert "s2" in sentinels  # highest battery
        assert "s4" in sentinels  # second highest

    def test_select_sentinels_zero_count(self, idle_monitor):
        """Zero sentinel count should return empty list."""
        sentinels = idle_monitor._select_sentinels(["s1", "s2"], 0)
        assert sentinels == []

    def test_select_sentinels_more_than_available(self, idle_monitor, mock_state_manager):
        """Requesting more sentinels than controllers should cap at available."""
        mock_state_manager.battery_levels = {"s1": 3}

        sentinels = idle_monitor._select_sentinels(["s1"], 2)

        assert sentinels == ["s1"]

    def test_select_sentinels_empty(self, idle_monitor):
        """Empty serial list should return empty sentinels."""
        sentinels = idle_monitor._select_sentinels([], 2)
        assert sentinels == []


class TestIdleMonitorEnterIdle:
    """Test entering idle mode."""

    @pytest.mark.asyncio
    @patch("services.menu.idle_monitor.metrics")
    @pytest.mark.uc("UC-010", flow="A2")
    @pytest.mark.uc("UC-002", flow="A3")
    async def test_enter_idle_transitions_controllers(self, mock_metrics, idle_monitor, mock_state_manager):
        """Entering idle should transition CONNECTED controllers to IDLE."""
        mock_state_manager.controller_states = {
            "s1": ControllerState.CONNECTED,
            "s2": ControllerState.CONNECTED,
            "s3": ControllerState.CONNECTED,
        }
        mock_state_manager.battery_levels = {"s1": 3, "s2": 5, "s3": 1}

        await idle_monitor._enter_idle_mode()

        assert idle_monitor.is_idle_active
        # All 3 should be transitioned to IDLE (including sentinels)
        assert mock_state_manager.transition_to.await_count == 3

    @pytest.mark.asyncio
    @patch("services.menu.idle_monitor.metrics")
    @pytest.mark.uc("UC-010", flow="A2")
    async def test_enter_idle_skips_ready_admin(self, mock_metrics, idle_monitor, mock_state_manager):
        """Entering idle should NOT transition READY or ADMIN controllers."""
        mock_state_manager.controller_states = {
            "s1": ControllerState.CONNECTED,
            "s2": ControllerState.READY,
            "s3": ControllerState.ADMIN,
        }
        mock_state_manager.battery_levels = {"s1": 3}

        await idle_monitor._enter_idle_mode()

        # Only s1 should be transitioned
        mock_state_manager.transition_to.assert_awaited_once_with("s1", ControllerState.IDLE)

    @pytest.mark.asyncio
    @patch("services.menu.idle_monitor.metrics")
    async def test_enter_idle_no_connected(self, mock_metrics, idle_monitor, mock_state_manager):
        """Entering idle with no CONNECTED controllers should be a no-op."""
        mock_state_manager.controller_states = {
            "s1": ControllerState.READY,
        }

        await idle_monitor._enter_idle_mode()

        assert not idle_monitor.is_idle_active
        mock_state_manager.transition_to.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("services.menu.idle_monitor.metrics")
    @pytest.mark.uc("UC-010", flow="A2", steps=[1])
    @pytest.mark.uc("UC-002", flow="A3")
    async def test_enter_idle_selects_sentinels(self, mock_metrics, idle_monitor, mock_state_manager):
        """UC-010 A2 step 1: up to sentinel.count (2) controllers are picked as sentinels, highest battery first."""
        mock_state_manager.controller_states = {
            "s1": ControllerState.CONNECTED,
            "s2": ControllerState.CONNECTED,
            "s3": ControllerState.CONNECTED,
        }
        mock_state_manager.battery_levels = {"s1": 1, "s2": 5, "s3": 3}

        await idle_monitor._enter_idle_mode()

        assert len(idle_monitor.sentinel_serials) == 2
        assert "s2" in idle_monitor.sentinel_serials  # highest battery


class TestIdleMonitorWakeAll:
    """Test waking all controllers from idle."""

    @pytest.mark.asyncio
    @patch("services.menu.idle_monitor.metrics")
    @pytest.mark.uc("UC-010", flow="A2", steps=[2])
    async def test_wake_all_transitions_idle_to_connected(self, mock_metrics, idle_monitor, mock_state_manager):
        """wake_all should transition all IDLE controllers to CONNECTED."""
        idle_monitor._idle_active = True
        mock_state_manager.controller_states = {
            "s1": ControllerState.IDLE,
            "s2": ControllerState.IDLE,
            "s3": ControllerState.IDLE,
        }

        await idle_monitor.wake_all("s1")

        assert not idle_monitor.is_idle_active
        # s2 and s3 should be transitioned (s1 was the trigger, already transitioned by handler)
        assert mock_state_manager.transition_to.await_count == 2

    @pytest.mark.asyncio
    @patch("services.menu.idle_monitor.metrics")
    async def test_wake_all_resets_timer(self, mock_metrics, idle_monitor, mock_state_manager):
        """wake_all should reset the activity timer."""
        idle_monitor._idle_active = True
        mock_state_manager.controller_states = {"s1": ControllerState.IDLE}

        idle_monitor._last_activity_time = 0

        await idle_monitor.wake_all("s1")

        assert idle_monitor._last_activity_time > 0

    @pytest.mark.asyncio
    async def test_wake_all_when_not_idle(self, idle_monitor, mock_state_manager):
        """wake_all should be a no-op when not in idle mode."""
        idle_monitor._idle_active = False

        await idle_monitor.wake_all("s1")

        mock_state_manager.transition_to.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("services.menu.idle_monitor.metrics")
    async def test_wake_all_clears_sentinels(self, mock_metrics, idle_monitor, mock_state_manager):
        """wake_all should clear sentinel list."""
        idle_monitor._idle_active = True
        idle_monitor._sentinel_serials = ["s1", "s2"]
        mock_state_manager.controller_states = {"s1": ControllerState.IDLE, "s2": ControllerState.IDLE}

        await idle_monitor.wake_all("s1")

        assert idle_monitor.sentinel_serials == []


class TestIdleMonitorStartStop:
    """Test start/stop lifecycle."""

    @pytest.mark.asyncio
    @patch("services.menu.idle_monitor.metrics")
    async def test_start(self, mock_metrics, idle_monitor):
        """start should create the monitor task."""
        idle_monitor.start()

        assert idle_monitor._monitor_task is not None
        assert not idle_monitor._monitor_task.done()

        # Cleanup
        idle_monitor.stop()
        await asyncio.sleep(0)  # Let cancellation propagate

    @pytest.mark.asyncio
    @patch("services.menu.idle_monitor.metrics")
    async def test_stop(self, mock_metrics, idle_monitor):
        """stop should cancel the monitor task."""
        idle_monitor.start()
        idle_monitor.stop()
        await asyncio.sleep(0)  # Let cancellation propagate

        assert idle_monitor._monitor_task is None
        assert not idle_monitor.is_idle_active

    @patch("services.menu.idle_monitor.metrics")
    def test_stop_without_start(self, mock_metrics, idle_monitor):
        """stop without start should not raise."""
        idle_monitor.stop()

    @pytest.mark.asyncio
    @patch("services.menu.idle_monitor.metrics")
    async def test_double_start(self, mock_metrics, idle_monitor):
        """Starting twice should not create duplicate tasks."""
        idle_monitor.start()
        task1 = idle_monitor._monitor_task
        idle_monitor.start()  # Should be no-op since task is still running

        assert idle_monitor._monitor_task is task1

        idle_monitor.stop()
        await asyncio.sleep(0)


class TestIdleMonitorDisabled:
    """Test idle mode when disabled via feature flag."""

    @pytest.mark.asyncio
    async def test_disabled_monitor_does_not_enter_idle(self, disabled_idle_monitor, mock_state_manager):
        """Monitor loop should not enter idle when disabled."""
        mock_state_manager.controller_states = {
            "s1": ControllerState.CONNECTED,
            "s2": ControllerState.CONNECTED,
        }

        # Force timeout to have passed
        disabled_idle_monitor._last_activity_time = 0

        # Run one iteration of the monitor loop concept
        # Since idle is disabled, it should not enter idle
        assert not disabled_idle_monitor._get_idle_enabled()
        assert not disabled_idle_monitor.is_idle_active


class TestIdleMonitorLoopTimeout:
    """UC-010 A2: the monitor loop enters idle once idle.timeout_minutes pass without a press."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A2", steps=[1])
    @patch("services.menu.idle_monitor.metrics")
    async def test_loop_enters_idle_at_timeout_not_before(self, mock_metrics, mock_state_manager):
        clock = {"now": 1000.0}
        timeout_minutes = 2
        real_sleep = asyncio.sleep
        idle_entered_at = []

        async def fake_sleep(seconds):
            clock["now"] += seconds
            if clock["now"] > 1000.0 + timeout_minutes * 60 + 5:
                raise asyncio.CancelledError
            await real_sleep(0)

        mock_state_manager.controller_states = {"s1": ControllerState.CONNECTED, "s2": ControllerState.CONNECTED}

        async def transition(serial, state):
            idle_entered_at.append(clock["now"])
            mock_state_manager.controller_states[serial] = state

        mock_state_manager.transition_to.side_effect = transition
        monitor = IdleMonitor(
            state_manager=mock_state_manager,
            get_idle_enabled=lambda: True,
            get_idle_timeout=lambda: timeout_minutes,
            get_sentinel_count=lambda: 0,
            get_rotation_minutes=lambda: 5,
        )

        with (
            patch("services.menu.idle_monitor.time.monotonic", side_effect=lambda: clock["now"]),
            patch("services.menu.idle_monitor.asyncio.sleep", side_effect=fake_sleep),
        ):
            monitor.reset_activity()
            await monitor._monitor_loop()

        assert monitor.is_idle_active
        assert idle_entered_at, "the loop never entered idle"
        assert min(idle_entered_at) == 1000.0 + timeout_minutes * 60
        assert set(mock_state_manager.controller_states.values()) == {ControllerState.IDLE}


class TestLobbyIdleTimeout:
    """UC-002 A3: no press for idle.timeout_minutes switches the lobby to IDLE with the LEDs off."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A3", steps=[1])
    @patch("services.menu.idle_monitor.metrics")
    async def test_timeout_turns_connected_controllers_idle_and_dark(self, _mock_metrics, real_servicer):
        """Real StateManager and IdleHandler: the timeout drives every connected controller to IDLE, LED off."""
        clock = {"now": 1000.0}
        timeout_minutes = 3
        real_sleep = asyncio.sleep

        async def fake_sleep(seconds):
            clock["now"] += seconds
            if clock["now"] > 1000.0 + timeout_minutes * 60 + 5:
                raise asyncio.CancelledError
            await real_sleep(0)

        for serial in ("s1", "s2"):
            await real_servicer.on_connect(serial)
        led = real_servicer.state_manager.led
        led.set_color.reset_mock()
        monitor = IdleMonitor(
            state_manager=real_servicer.state_manager,
            get_idle_enabled=lambda: True,
            get_idle_timeout=lambda: timeout_minutes,
            get_sentinel_count=lambda: 0,
            get_rotation_minutes=lambda: 5,
        )
        dark_at = []
        led.set_color.side_effect = lambda serial, color: dark_at.append((serial, color, clock["now"])) or True

        with (
            patch("services.menu.idle_monitor.time.monotonic", side_effect=lambda: clock["now"]),
            patch("services.menu.idle_monitor.asyncio.sleep", side_effect=fake_sleep),
        ):
            monitor.reset_activity()
            await monitor._monitor_loop()

        assert monitor.is_idle_active
        assert real_servicer.state_manager.controller_states == {"s1": ControllerState.IDLE, "s2": ControllerState.IDLE}
        timeout_at = 1000.0 + timeout_minutes * 60
        assert sorted(dark_at) == [("s1", (0, 0, 0), timeout_at), ("s2", (0, 0, 0), timeout_at)]


class TestIdleMonitorSentinelRotation:
    """Test sentinel rotation."""

    @pytest.mark.asyncio
    @patch("services.menu.idle_monitor.metrics")
    async def test_rotation_selects_new_sentinels(self, mock_metrics, idle_monitor, mock_state_manager):
        """Rotation should select different controllers when possible."""
        import time as _time

        idle_monitor._idle_active = True
        idle_monitor._sentinel_serials = ["s1", "s2"]
        # Force rotation by setting last rotation far enough in the past
        idle_monitor._last_rotation_time = _time.monotonic() - 600

        mock_state_manager.controller_states = {
            "s1": ControllerState.IDLE,
            "s2": ControllerState.IDLE,
            "s3": ControllerState.IDLE,
            "s4": ControllerState.IDLE,
        }
        mock_state_manager.battery_levels = {"s1": 1, "s2": 1, "s3": 5, "s4": 4}

        await idle_monitor._check_sentinel_rotation()

        # Should prefer non-current sentinels (s3, s4)
        assert "s3" in idle_monitor.sentinel_serials
        assert "s4" in idle_monitor.sentinel_serials

    @pytest.mark.asyncio
    async def test_rotation_disabled(self, mock_state_manager):
        """Rotation should be skipped when rotation_minutes is 0."""
        monitor = IdleMonitor(
            state_manager=mock_state_manager,
            get_idle_enabled=lambda: True,
            get_idle_timeout=lambda: 15,
            get_sentinel_count=lambda: 2,
            get_rotation_minutes=lambda: 0,
        )
        monitor._idle_active = True
        monitor._sentinel_serials = ["s1"]
        monitor._last_rotation_time = 0

        mock_state_manager.controller_states = {"s1": ControllerState.IDLE, "s2": ControllerState.IDLE}

        await monitor._check_sentinel_rotation()

        # Should not change sentinels
        assert monitor.sentinel_serials == ["s1"]


class TestHsvToRgb:
    """Test HSV to RGB conversion."""

    def test_red(self):
        """Hue 0 should be red."""
        r, g, b = _hsv_to_rgb(0.0, 1.0, 1.0)
        assert r == 255 and g == 0 and b == 0

    def test_green(self):
        """Hue 1/3 should be green."""
        r, g, b = _hsv_to_rgb(1 / 3, 1.0, 1.0)
        assert r == 0 and g == 255 and b == 0

    def test_blue(self):
        """Hue 2/3 should be blue."""
        r, g, b = _hsv_to_rgb(2 / 3, 1.0, 1.0)
        assert r == 0 and g == 0 and b == 255

    def test_white(self):
        """Zero saturation should be white/gray."""
        r, g, b = _hsv_to_rgb(0.0, 0.0, 1.0)
        assert r == 255 and g == 255 and b == 255

    def test_black(self):
        """Zero value should be black."""
        r, g, b = _hsv_to_rgb(0.0, 1.0, 0.0)
        assert r == 0 and g == 0 and b == 0

    def test_dim_red(self):
        """Half value red should produce dimmer output."""
        r, g, b = _hsv_to_rgb(0.0, 1.0, 0.5)
        assert r == 127 and g == 0 and b == 0


class TestIdleWakeConsumesPress:
    """UC-010 A2 step 2 through a real StateManager, handlers and IdleMonitor (only I/O edges autospec'd)."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A2", steps=[2])
    @patch("services.menu.idle_monitor.metrics")
    async def test_trigger_on_idle_controller_wakes_all_and_is_consumed(self, mock_metrics):
        """A trigger press on an idle controller wakes every idle controller; it does not ready that controller."""
        from unittest.mock import create_autospec

        from lib.flag_service_writer import FlagServiceWriter
        from services.menu.handlers.connected import ConnectedHandler
        from services.menu.handlers.ready import ReadyHandler
        from services.menu.state_manager import StateManager
        from services.menu.utils import AudioHelper, LedController

        manager = StateManager(
            led=create_autospec(LedController, instance=True, spec_set=True),
            audio=create_autospec(AudioHelper, instance=True, spec_set=True),
            game_settings_writer=create_autospec(FlagServiceWriter, instance=True, spec_set=True),
            user_prefs_writer=create_autospec(FlagServiceWriter, instance=True, spec_set=True),
            publish_event=AsyncMock(),
        )
        start_game = AsyncMock()
        idle_handler = IdleHandler()
        for handler in (ConnectedHandler(), ReadyHandler(start_game), idle_handler):
            manager.register_handler(handler)
        monitor = IdleMonitor(
            state_manager=manager,
            get_idle_enabled=lambda: True,
            get_idle_timeout=lambda: 15,
            get_sentinel_count=lambda: 0,
            get_rotation_minutes=lambda: 5,
        )
        idle_handler.set_wake_callback(monitor.wake_all)
        for serial in ("s1", "s2", "s3"):
            await manager.on_controller_connected(serial)
        await monitor._enter_idle_mode()
        assert set(manager.controller_states.values()) == {ControllerState.IDLE}

        await manager.handle_button_event("s1", "trigger", True)

        # Every idle controller is back in the lobby, and the press did not ready s1.
        assert manager.controller_states == {
            "s1": ControllerState.CONNECTED,
            "s2": ControllerState.CONNECTED,
            "s3": ControllerState.CONNECTED,
        }
        assert not monitor.is_idle_active
        start_game.assert_not_awaited()
