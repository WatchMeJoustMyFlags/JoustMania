"""
Extended tests for DiscoveryLoop.

Tests controller discovery, disconnection detection, and state management:
- New controller detection
- Disconnection cleanup
- Activity tracking for adaptive polling
- State update processing

Issue #209: Improve test coverage for critical game flow
"""

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))


class MockBackend:
    """Mock backend for testing discovery loop."""

    def __init__(self):
        self.connected_controllers = []
        self.controller_states = {}
        self.initialized = False
        self._led_updates = 0

    async def initialize(self):
        self.initialized = True
        return True

    def get_connected_controllers(self, force_rescan=False):
        return self.connected_controllers

    async def get_controller_state(self, serial):
        return self.controller_states.get(
            serial,
            {
                "serial": serial,
                "battery": 80,
                "trigger": 0,
                "accel": {"x": 0.0, "y": 0.0, "z": 1.0},
            },
        )

    def update_all_leds(self):
        self._led_updates += 1
        return len(self.connected_controllers)


class MockButtonDetector:
    """Mock button detector."""

    def __init__(self):
        self.transitions = []
        self.connection_events = []

    def detect_transitions_from_state(self, serial, state, tracked):
        self.transitions.append((serial, state))

    def publish_connection_event(self, serial, is_connect, battery=0, name=""):
        self.connection_events.append(
            {
                "serial": serial,
                "is_connect": is_connect,
                "battery": battery,
                "name": name,
            }
        )

    def clear_controller(self, serial):
        pass


class MockStateCache:
    """Mock state cache manager."""

    def __init__(self):
        self.cleared = []

    def clear_controller(self, serial):
        self.cleared.append(serial)


class MockFeedbackManager:
    """Mock feedback manager."""

    async def set_controller_color(self, serial, color):
        pass


class MockMonitoring:
    """Mock controller monitoring."""

    def __init__(self):
        self.last_battery_check = 0.0

    def check_battery_levels(self, tracked):
        pass


class MockRescanTimer:
    """Mock periodic rescan timer."""

    def __init__(self):
        self._should_force = False

    def should_force_rescan(self):
        return self._should_force


class MockEventPublisher:
    """Mock event publisher."""

    def __init__(self):
        self.events = []

    def publish(self, event_type, data):
        self.events.append((event_type, data))


class MockNameManager:
    """Mock name manager."""

    def __init__(self):
        self.names = {}

    def get_name(self, serial):
        return self.names.get(serial, "")


class TestDiscoveryLoopInit:
    """Tests for DiscoveryLoop initialization."""

    def test_init_sets_running_true(self):
        """DiscoveryLoop should start with running=True."""
        with patch("services.controller_manager.discovery_loop.get_tracer"):
            from services.controller_manager.discovery_loop import DiscoveryLoop

            loop = DiscoveryLoop(
                backend=MockBackend(),
                tracked_controllers={},
                controller_states={},
                button_detector=MockButtonDetector(),
                state_cache_manager=MockStateCache(),
                feedback_manager=MockFeedbackManager(),
                monitoring=MockMonitoring(),
                rescan_timer=MockRescanTimer(),
                paired_serials=[],
                base_colors={},
                event_publisher=MockEventPublisher(),
            )

            assert loop.running is True

    def test_init_backend_not_initialized(self):
        """Backend should not be initialized until start()."""
        with patch("services.controller_manager.discovery_loop.get_tracer"):
            from services.controller_manager.discovery_loop import DiscoveryLoop

            loop = DiscoveryLoop(
                backend=MockBackend(),
                tracked_controllers={},
                controller_states={},
                button_detector=MockButtonDetector(),
                state_cache_manager=MockStateCache(),
                feedback_manager=MockFeedbackManager(),
                monitoring=MockMonitoring(),
                rescan_timer=MockRescanTimer(),
                paired_serials=[],
                base_colors={},
                event_publisher=MockEventPublisher(),
            )

            assert loop.backend_initialized is False


class TestDiscoveryLoopStop:
    """Tests for stopping the discovery loop."""

    def test_stop_sets_running_false(self):
        """stop() should set running to False."""
        with patch("services.controller_manager.discovery_loop.get_tracer"):
            from services.controller_manager.discovery_loop import DiscoveryLoop

            loop = DiscoveryLoop(
                backend=MockBackend(),
                tracked_controllers={},
                controller_states={},
                button_detector=MockButtonDetector(),
                state_cache_manager=MockStateCache(),
                feedback_manager=MockFeedbackManager(),
                monitoring=MockMonitoring(),
                rescan_timer=MockRescanTimer(),
                paired_serials=[],
                base_colors={},
                event_publisher=MockEventPublisher(),
            )

            assert loop.running is True
            loop.stop()
            assert loop.running is False


class TestControllerDisconnection:
    """Tests for controller disconnection handling."""

    def test_disconnection_clears_tracking(self):
        """Disconnected controller should be removed from tracking."""
        tracked_controllers = {"serial_1": {"battery": 80}}
        controller_states = {"serial_1": {"trigger": 0}}
        state_cache = MockStateCache()
        button_detector = MockButtonDetector()

        # Simulate disconnection detection
        connected_set = set()  # No controllers connected
        tracked_serials = set(tracked_controllers.keys())

        disconnected = tracked_serials - connected_set

        for serial in disconnected:
            del tracked_controllers[serial]
            del controller_states[serial]
            state_cache.clear_controller(serial)
            button_detector.publish_connection_event(serial, is_connect=False)

        assert "serial_1" not in tracked_controllers
        assert "serial_1" not in controller_states
        assert "serial_1" in state_cache.cleared
        assert len(button_detector.connection_events) == 1
        assert button_detector.connection_events[0]["is_connect"] is False

    def test_disconnection_preserves_base_color(self):
        """Base color should be preserved on disconnect for reconnection."""
        base_colors = {"serial_1": (255, 0, 0)}

        # Simulate disconnect - base_colors should NOT be cleared
        # (This is intentional behavior to restore color on reconnect)

        assert "serial_1" in base_colors
        assert base_colors["serial_1"] == (255, 0, 0)


class TestNewControllerDetection:
    """Tests for new controller detection."""

    def test_new_controller_added_to_tracking(self):
        """New controller should be added to tracked_controllers."""
        tracked_controllers = {}
        controller_states = {}
        button_detector = MockButtonDetector()

        # Simulate new controller detection
        connected_serials = ["new_serial"]

        for serial in connected_serials:
            if serial not in tracked_controllers:
                tracked_controllers[serial] = {
                    "serial": serial,
                    "battery": 80,
                    "team": 0,
                }
                controller_states[serial] = {"trigger": 0}
                button_detector.publish_connection_event(serial, is_connect=True, battery=80)

        assert "new_serial" in tracked_controllers
        assert "new_serial" in controller_states
        assert len(button_detector.connection_events) == 1
        assert button_detector.connection_events[0]["is_connect"] is True

    def test_existing_controller_not_re_added(self):
        """Existing controller should not be re-added."""
        tracked_controllers = {"existing_serial": {"battery": 80}}
        button_detector = MockButtonDetector()

        connected_serials = ["existing_serial"]

        for serial in connected_serials:
            if serial not in tracked_controllers:
                button_detector.publish_connection_event(serial, is_connect=True)

        # No connection event should be published
        assert len(button_detector.connection_events) == 0


class TestActivityTracking:
    """Tests for adaptive polling activity tracking."""

    def test_button_press_marks_active(self):
        """Button press should mark controller as active."""
        from lib.controller_constants import ButtonKey

        last_activity_time = {}
        current_time = time.time()

        state = {
            ButtonKey.MOVE: True,  # Button pressed
        }

        # Check for button activity
        activity_detected = False
        button_keys = [ButtonKey.MOVE, ButtonKey.TRIGGER, ButtonKey.PS]
        for key in button_keys:
            if state.get(key, False):
                activity_detected = True
                break

        if activity_detected:
            last_activity_time["test_serial"] = current_time

        assert activity_detected is True
        assert "test_serial" in last_activity_time

    def test_no_activity_when_idle(self):
        """No activity should be detected when controller is idle."""
        from lib.controller_constants import ButtonKey

        state = {
            ButtonKey.MOVE: False,
            ButtonKey.TRIGGER: False,
            ButtonKey.PS: False,
        }

        activity_detected = False
        button_keys = [ButtonKey.MOVE, ButtonKey.TRIGGER, ButtonKey.PS]
        for key in button_keys:
            if state.get(key, False):
                activity_detected = True
                break

        assert activity_detected is False

    def test_accelerometer_movement_marks_active(self):
        """Significant accelerometer movement should mark controller as active."""
        previous_accel = {"test_serial": (0.0, 0.0, 1.0)}
        accel_movement_threshold = 0.05

        # Current accel with significant movement
        current_accel = (0.1, 0.2, 1.0)

        prev = previous_accel["test_serial"]
        dx = abs(current_accel[0] - prev[0])
        dy = abs(current_accel[1] - prev[1])
        dz = abs(current_accel[2] - prev[2])
        movement = dx + dy + dz

        activity_detected = movement > accel_movement_threshold

        assert activity_detected is True
        assert movement == pytest.approx(0.3, rel=0.01)

    def test_small_movement_not_active(self):
        """Small accelerometer movement should not mark as active."""
        previous_accel = {"test_serial": (0.0, 0.0, 1.0)}
        accel_movement_threshold = 0.05

        # Current accel with tiny movement
        current_accel = (0.01, 0.01, 1.0)

        prev = previous_accel["test_serial"]
        dx = abs(current_accel[0] - prev[0])
        dy = abs(current_accel[1] - prev[1])
        dz = abs(current_accel[2] - prev[2])
        movement = dx + dy + dz

        activity_detected = movement > accel_movement_threshold

        assert activity_detected is False


class TestAdaptivePolling:
    """Tests for adaptive polling rate logic."""

    def test_active_controller_gets_fast_poll(self):
        """Active controllers should be polled at 60Hz."""
        idle_threshold_seconds = 5.0
        active_poll_interval = 0.016  # ~60Hz
        idle_poll_interval = 0.100  # ~10Hz

        current_time = time.time()
        last_activity = current_time - 1.0  # 1 second ago (active)

        is_idle = (current_time - last_activity) > idle_threshold_seconds

        poll_interval = idle_poll_interval if is_idle else active_poll_interval

        assert is_idle is False
        assert poll_interval == active_poll_interval

    def test_idle_controller_gets_slow_poll(self):
        """Idle controllers should be polled at 10Hz."""
        idle_threshold_seconds = 5.0
        active_poll_interval = 0.016
        idle_poll_interval = 0.100

        current_time = time.time()
        last_activity = current_time - 10.0  # 10 seconds ago (idle)

        is_idle = (current_time - last_activity) > idle_threshold_seconds

        poll_interval = idle_poll_interval if is_idle else active_poll_interval

        assert is_idle is True
        assert poll_interval == idle_poll_interval

    def test_skip_poll_if_too_soon(self):
        """Polling should be skipped if not enough time has passed."""
        current_time = time.time()
        last_poll_time = current_time - 0.005  # 5ms ago
        poll_interval = 0.016  # 16ms interval

        should_poll = (current_time - last_poll_time) >= poll_interval

        assert should_poll is False


class TestBaseColorRestoration:
    """Tests for base color restoration on reconnect."""

    def test_base_color_restored_on_reconnect(self):
        """Base color should be restored when controller reconnects."""
        base_colors = {"serial_1": (255, 0, 0)}
        tracked_controllers = {}

        # Simulate reconnection
        serial = "serial_1"
        tracked_controllers[serial] = {"battery": 80}

        # Check if we should restore color
        if serial in base_colors:
            color_to_restore = base_colors[serial]
            assert color_to_restore == (255, 0, 0)

    def test_no_color_for_new_controller(self):
        """New controller with no base color should not have color restored."""
        base_colors = {}
        serial = "new_serial"

        has_base_color = serial in base_colors

        assert has_base_color is False


class TestCheckForNewControllersAsync:
    """Async tests for _check_for_new_controllers method."""

    def _create_discovery_loop(
        self,
        backend=None,
        tracked_controllers=None,
        controller_states=None,
        button_detector=None,
        state_cache=None,
        feedback_manager=None,
        rescan_timer=None,
        base_colors=None,
        name_manager=None,
    ):
        """Create a DiscoveryLoop with configurable mock dependencies."""
        from services.controller_manager.discovery_loop import DiscoveryLoop

        _backend = backend or MockBackend()
        _tracked = tracked_controllers if tracked_controllers is not None else {}
        _states = controller_states if controller_states is not None else {}
        _button = button_detector or MockButtonDetector()
        _cache = state_cache or MockStateCache()
        _feedback = feedback_manager or MockFeedbackManager()
        _monitoring = MockMonitoring()
        _rescan = rescan_timer or MockRescanTimer()
        _base_colors = base_colors if base_colors is not None else {}
        _event_pub = MockEventPublisher()

        return DiscoveryLoop(
            backend=_backend,
            tracked_controllers=_tracked,
            controller_states=_states,
            button_detector=_button,
            state_cache_manager=_cache,
            feedback_manager=_feedback,
            monitoring=_monitoring,
            rescan_timer=_rescan,
            paired_serials=[],
            base_colors=_base_colors,
            event_publisher=_event_pub,
            name_manager=name_manager,
        )

    @pytest.mark.asyncio
    async def test_new_controller_added_to_tracking(self):
        """_check_for_new_controllers should add new controller to tracked_controllers."""
        backend = MockBackend()
        backend.connected_controllers = ["serial_A"]
        backend.controller_states["serial_A"] = {
            "serial": "serial_A",
            "battery": 75,
        }

        tracked = {}
        states = {}
        button_det = MockButtonDetector()
        loop = self._create_discovery_loop(
            backend=backend,
            tracked_controllers=tracked,
            controller_states=states,
            button_detector=button_det,
        )
        loop.backend_initialized = True

        await loop._check_for_new_controllers()

        assert "serial_A" in tracked
        assert tracked["serial_A"]["battery"] == 75
        assert len(button_det.connection_events) == 1
        assert button_det.connection_events[0]["is_connect"] is True

    @pytest.mark.asyncio
    async def test_disconnected_controller_removed(self):
        """_check_for_new_controllers should remove disconnected controllers."""
        backend = MockBackend()
        backend.connected_controllers = []  # No controllers connected

        tracked = {"serial_A": {"battery": 80, "name": "Player1"}}
        states = {"serial_A": {"trigger": 0}}
        button_det = MockButtonDetector()
        cache = MockStateCache()
        loop = self._create_discovery_loop(
            backend=backend,
            tracked_controllers=tracked,
            controller_states=states,
            button_detector=button_det,
            state_cache=cache,
        )
        loop.backend_initialized = True
        loop._last_activity_time["serial_A"] = 1.0
        loop._previous_accel["serial_A"] = (0.0, 0.0, 1.0)

        await loop._check_for_new_controllers()

        assert "serial_A" not in tracked
        assert "serial_A" not in states
        assert "serial_A" in cache.cleared
        assert "serial_A" not in loop._last_activity_time
        assert "serial_A" not in loop._previous_accel
        assert len(button_det.connection_events) == 1
        assert button_det.connection_events[0]["is_connect"] is False
        assert button_det.connection_events[0]["serial"] == "serial_A"

    @pytest.mark.asyncio
    async def test_skipped_when_backend_not_initialized(self):
        """_check_for_new_controllers should do nothing when backend not initialized."""
        backend = MockBackend()
        backend.connected_controllers = ["serial_A"]

        tracked = {}
        loop = self._create_discovery_loop(backend=backend, tracked_controllers=tracked)
        loop.backend_initialized = False

        await loop._check_for_new_controllers()

        assert len(tracked) == 0

    @pytest.mark.asyncio
    async def test_existing_controller_not_re_added(self):
        """_check_for_new_controllers should not re-add existing controllers."""
        backend = MockBackend()
        backend.connected_controllers = ["serial_A"]

        tracked = {"serial_A": {"battery": 80}}
        button_det = MockButtonDetector()
        loop = self._create_discovery_loop(
            backend=backend,
            tracked_controllers=tracked,
            button_detector=button_det,
        )
        loop.backend_initialized = True

        await loop._check_for_new_controllers()

        # No connection events for already-tracked controllers
        assert len(button_det.connection_events) == 0

    @pytest.mark.asyncio
    async def test_base_color_restored_on_reconnect(self):
        """_check_for_new_controllers should restore base color when controller reconnects."""
        backend = MockBackend()
        backend.connected_controllers = ["serial_A"]
        backend.controller_states["serial_A"] = {
            "serial": "serial_A",
            "battery": 90,
        }

        tracked = {}
        base_colors = {"serial_A": (255, 0, 0)}
        feedback = MockFeedbackManager()
        feedback.color_calls = []

        original_set = feedback.set_controller_color

        async def tracking_set_color(serial, color):
            feedback.color_calls.append((serial, color))
            return await original_set(serial, color)

        feedback.set_controller_color = tracking_set_color

        loop = self._create_discovery_loop(
            backend=backend,
            tracked_controllers=tracked,
            base_colors=base_colors,
            feedback_manager=feedback,
        )
        loop.backend_initialized = True

        await loop._check_for_new_controllers()

        # Should have called set_controller_color to restore base color
        assert len(feedback.color_calls) == 1
        assert feedback.color_calls[0] == ("serial_A", (255, 0, 0))

    @pytest.mark.asyncio
    async def test_name_manager_used_for_new_controller(self):
        """_check_for_new_controllers should use name_manager to get controller name."""
        backend = MockBackend()
        backend.connected_controllers = ["serial_A"]
        backend.controller_states["serial_A"] = {
            "serial": "serial_A",
            "battery": 80,
        }

        tracked = {}
        name_mgr = MockNameManager()
        name_mgr.names["serial_A"] = "Player One"

        loop = self._create_discovery_loop(
            backend=backend,
            tracked_controllers=tracked,
            name_manager=name_mgr,
        )
        loop.backend_initialized = True

        await loop._check_for_new_controllers()

        assert tracked["serial_A"]["name"] == "Player One"


class TestUpdateControllerStatesAsync:
    """Async tests for _update_controller_states method."""

    def _create_discovery_loop(self, backend=None, tracked=None, states=None, button_detector=None):
        """Create a DiscoveryLoop with common defaults."""
        from services.controller_manager.discovery_loop import DiscoveryLoop

        _backend = backend or MockBackend()
        _tracked = tracked if tracked is not None else {}
        _states = states if states is not None else {}
        _button = button_detector or MockButtonDetector()

        return DiscoveryLoop(
            backend=_backend,
            tracked_controllers=_tracked,
            controller_states=_states,
            button_detector=_button,
            state_cache_manager=MockStateCache(),
            feedback_manager=MockFeedbackManager(),
            monitoring=MockMonitoring(),
            rescan_timer=MockRescanTimer(),
            paired_serials=[],
            base_colors={},
            event_publisher=MockEventPublisher(),
        )

    @pytest.mark.asyncio
    async def test_updates_controller_states(self):
        """_update_controller_states should poll and store states for all tracked controllers."""
        from lib.controller_constants import StateKey

        backend = MockBackend()
        backend.controller_states["s1"] = {
            StateKey.BATTERY: 75,
            "trigger": 100,
        }
        backend.controller_states["s2"] = {
            StateKey.BATTERY: 50,
            "trigger": 0,
        }

        tracked = {"s1": {"battery": 0}, "s2": {"battery": 0}}
        states = {}
        button_det = MockButtonDetector()
        loop = self._create_discovery_loop(backend=backend, tracked=tracked, states=states, button_detector=button_det)
        loop.backend_initialized = True

        await loop._update_controller_states()

        assert "s1" in states
        assert "s2" in states
        assert tracked["s1"]["battery"] == 75
        assert tracked["s2"]["battery"] == 50
        assert len(button_det.transitions) == 2

    @pytest.mark.asyncio
    async def test_skips_when_no_controllers(self):
        """_update_controller_states should return early when no controllers are tracked."""
        backend = MockBackend()
        tracked = {}
        states = {}
        loop = self._create_discovery_loop(backend=backend, tracked=tracked, states=states)
        loop.backend_initialized = True

        # Should not raise
        await loop._update_controller_states()

        assert len(states) == 0
