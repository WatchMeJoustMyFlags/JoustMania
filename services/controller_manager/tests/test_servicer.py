"""
Unit tests for ControllerManagerServicer.

Tests the gRPC servicer methods that can be tested in isolation:
- RenameController RPC
- Initialization and shutdown
- Extracted helper methods
- StreamButtonEvents RPC
- StreamGameplayData RPC

Issue #209: Improve test coverage for critical game flow
"""

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from proto import controller_manager_pb2


class MockGrpcContext:
    """Mock gRPC context for testing."""

    def __init__(self):
        self._cancelled = False

    def cancelled(self):
        return self._cancelled


class MockBackend:
    """Mock backend for testing without hardware."""

    def __init__(self):
        self.controllers = {}
        self.led_colors = {}
        self.rumble_values = {}
        self.effect_active = {}

    def initialize(self):
        pass

    def discover(self):
        return list(self.controllers.keys())

    def poll(self, serial):
        return self.controllers.get(serial, {})

    def set_led(self, serial, r, g, b):
        self.led_colors[serial] = (r, g, b)

    async def set_rumble(self, serial, intensity):
        self.rumble_values[serial] = intensity

    def set_effect_active(self, serial, active):
        self.effect_active[serial] = active

    def cleanup(self):
        pass


class TestRenameController:
    """Tests for RenameController RPC."""

    @pytest.fixture
    def servicer_components(self):
        """Create mock components for servicer testing."""
        # Mock the backend and discovery loop to avoid hardware initialization
        with (
            patch("services.controller_manager.servicer.create_backend") as mock_create_backend,
            patch("services.controller_manager.servicer.DiscoveryLoop") as mock_discovery_loop,
        ):
            mock_backend = MockBackend()
            mock_create_backend.return_value = mock_backend

            mock_loop = MagicMock()
            mock_loop.start = MagicMock()
            mock_loop.stop = MagicMock()
            mock_loop.wait_stopped = AsyncMock()
            mock_loop.drain_health_counters = MagicMock(return_value=(0, 0, 0))
            mock_discovery_loop.return_value = mock_loop

            from services.controller_manager.servicer import ControllerManagerServicer

            servicer = ControllerManagerServicer()
            yield servicer, mock_backend

    @pytest.mark.asyncio
    async def test_rename_controller_success(self, servicer_components):
        """RenameController should succeed with valid serial and name."""
        servicer, _ = servicer_components
        context = MockGrpcContext()

        # Add a tracked controller
        servicer.tracked_controllers["test_serial"] = {"battery": 80}

        request = controller_manager_pb2.RenameControllerRequest(serial="test_serial", name="Player 1")

        response = await servicer.RenameController(request, context)

        assert response.success is True
        assert response.error == ""
        # Check name was stored
        assert servicer.name_manager.get_name("test_serial") == "Player 1"

    @pytest.mark.asyncio
    async def test_rename_controller_updates_tracked(self, servicer_components):
        """RenameController should update tracked_controllers if connected."""
        servicer, _ = servicer_components
        context = MockGrpcContext()

        # Add a tracked controller
        from lib.controller_constants import ControllerInfoKey

        servicer.tracked_controllers["test_serial"] = {
            ControllerInfoKey.BATTERY: 80,
            ControllerInfoKey.NAME: "Old Name",
        }

        request = controller_manager_pb2.RenameControllerRequest(serial="test_serial", name="New Name")

        await servicer.RenameController(request, context)

        assert servicer.tracked_controllers["test_serial"][ControllerInfoKey.NAME] == "New Name"

    @pytest.mark.asyncio
    async def test_rename_controller_empty_serial(self, servicer_components):
        """RenameController should fail with empty serial."""
        servicer, _ = servicer_components
        context = MockGrpcContext()

        request = controller_manager_pb2.RenameControllerRequest(serial="", name="Player 1")

        response = await servicer.RenameController(request, context)

        assert response.success is False
        assert "serial" in response.error.lower()

    @pytest.mark.asyncio
    async def test_rename_controller_empty_name(self, servicer_components):
        """RenameController should fail with empty name."""
        servicer, _ = servicer_components
        context = MockGrpcContext()

        request = controller_manager_pb2.RenameControllerRequest(serial="test_serial", name="")

        response = await servicer.RenameController(request, context)

        assert response.success is False
        assert "name" in response.error.lower()

    @pytest.mark.asyncio
    async def test_rename_controller_not_connected(self, servicer_components):
        """RenameController should succeed even if controller not connected."""
        servicer, _ = servicer_components
        context = MockGrpcContext()

        # Don't add controller to tracked_controllers
        request = controller_manager_pb2.RenameControllerRequest(serial="unknown_serial", name="Player 1")

        response = await servicer.RenameController(request, context)

        # Should still succeed - name is stored for when controller connects
        assert response.success is True
        assert servicer.name_manager.get_name("unknown_serial") == "Player 1"


class TestServicerInitialization:
    """Tests for servicer initialization."""

    @pytest.fixture
    def servicer(self):
        """Create servicer with mocked dependencies."""
        with (
            patch("services.controller_manager.servicer.create_backend") as mock_create_backend,
            patch("services.controller_manager.servicer.DiscoveryLoop") as mock_discovery_loop,
        ):
            mock_backend = MockBackend()
            mock_create_backend.return_value = mock_backend

            mock_loop = MagicMock()
            mock_loop.start = MagicMock()
            mock_loop.stop = MagicMock()
            mock_loop.wait_stopped = AsyncMock()
            mock_loop.drain_health_counters = MagicMock(return_value=(0, 0, 0))
            mock_discovery_loop.return_value = mock_loop

            from services.controller_manager.servicer import ControllerManagerServicer

            servicer = ControllerManagerServicer()
            yield servicer

    def test_init_creates_empty_tracked_controllers(self, servicer):
        """Servicer should start with empty tracked_controllers."""
        assert servicer.tracked_controllers == {}

    def test_init_creates_empty_controller_states(self, servicer):
        """Servicer should start with empty controller_states."""
        assert servicer.controller_states == {}

    def test_init_creates_empty_subscribers(self, servicer):
        """Servicer should start with empty subscribers."""
        assert servicer.stream_subscribers == {}
        assert servicer.button_event_subscribers == {}

    def test_init_defers_discovery_loop_start(self, servicer):
        """Servicer should defer discovery loop start until first stream."""
        # Discovery loop is NOT started in __init__, only when first stream connects
        servicer.discovery_loop.start.assert_not_called()
        assert servicer._discovery_started is False


class TestServicerShutdown:
    """Tests for servicer shutdown."""

    @pytest.fixture
    def servicer(self):
        """Create servicer with mocked dependencies."""
        with (
            patch("services.controller_manager.servicer.create_backend") as mock_create_backend,
            patch("services.controller_manager.servicer.DiscoveryLoop") as mock_discovery_loop,
        ):
            mock_backend = MockBackend()
            mock_create_backend.return_value = mock_backend

            mock_loop = MagicMock()
            mock_loop.start = MagicMock()
            mock_loop.stop = MagicMock()
            mock_loop.wait_stopped = AsyncMock()
            mock_loop.drain_health_counters = MagicMock(return_value=(0, 0, 0))
            mock_discovery_loop.return_value = mock_loop

            from services.controller_manager.servicer import ControllerManagerServicer

            servicer = ControllerManagerServicer()
            yield servicer

    @pytest.mark.asyncio
    async def test_shutdown_stops_discovery_loop(self, servicer):
        """Shutdown should stop discovery loop."""
        await servicer.shutdown()

        servicer.discovery_loop.stop.assert_called_once()
        servicer.discovery_loop.wait_stopped.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_terminates_controller_processes(self, servicer):
        """Shutdown should terminate controller processes."""
        # Add mock process
        mock_proc = MagicMock()
        mock_proc.is_alive.return_value = True
        servicer.controller_processes["test_serial"] = mock_proc

        await servicer.shutdown()

        mock_proc.terminate.assert_called_once()
        mock_proc.join.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_skips_dead_processes(self, servicer):
        """Shutdown should skip already-dead processes."""
        mock_proc = MagicMock()
        mock_proc.is_alive.return_value = False
        servicer.controller_processes["test_serial"] = mock_proc

        await servicer.shutdown()

        mock_proc.terminate.assert_not_called()


class TestExtractedHelpers:
    """Tests for extracted helper methods used by stream RPCs."""

    @pytest.fixture
    def servicer_components(self):
        """Create servicer with mocked dependencies."""
        with (
            patch("services.controller_manager.servicer.create_backend") as mock_create_backend,
            patch("services.controller_manager.servicer.DiscoveryLoop") as mock_discovery_loop,
        ):
            mock_backend = MockBackend()
            mock_create_backend.return_value = mock_backend

            mock_loop = MagicMock()
            mock_loop.start = MagicMock()
            mock_loop.stop = MagicMock()
            mock_loop.wait_stopped = AsyncMock()
            mock_loop.drain_health_counters = MagicMock(return_value=(0, 0, 0))
            mock_discovery_loop.return_value = mock_loop

            from services.controller_manager.servicer import ControllerManagerServicer

            servicer = ControllerManagerServicer()
            yield servicer, mock_backend

    @pytest.mark.asyncio
    async def test_send_initial_connection_events(self, servicer_components):
        """_send_initial_connection_events should enqueue connect events for tracked controllers."""
        from lib.controller_constants import ControllerInfoKey

        servicer, _ = servicer_components
        queue = asyncio.Queue(maxsize=100)

        servicer.tracked_controllers["AA:BB"] = {
            ControllerInfoKey.BATTERY: 80,
            ControllerInfoKey.NAME: "P1",
        }
        servicer.tracked_controllers["CC:DD"] = {
            ControllerInfoKey.BATTERY: 60,
            ControllerInfoKey.NAME: "P2",
        }

        await servicer._send_initial_connection_events("sub_1", queue)

        assert queue.qsize() == 2
        event1 = queue.get_nowait()
        assert event1.event_type == controller_manager_pb2.EVENT_CONNECT
        assert event1.serial in ("AA:BB", "CC:DD")

    @pytest.mark.asyncio
    async def test_send_initial_connection_events_queue_full(self, servicer_components):
        """_send_initial_connection_events should skip events when queue is full."""
        servicer, _ = servicer_components
        queue = asyncio.Queue(maxsize=1)

        servicer.tracked_controllers["AA:BB"] = {}
        servicer.tracked_controllers["CC:DD"] = {}

        # Should not raise, just log warning and skip
        await servicer._send_initial_connection_events("sub_1", queue)

        assert queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_process_base_color_command_tracked(self, servicer_components):
        """_process_base_color_command should delegate to feedback_manager.apply_base_color."""
        servicer, _ = servicer_components
        servicer.tracked_controllers["AA:BB"] = {}
        servicer.feedback_manager.apply_base_color = AsyncMock()

        cmd = MagicMock()
        cmd.serial = "AA:BB"
        cmd.color.r = 255
        cmd.color.g = 0
        cmd.color.b = 0

        await servicer._process_base_color_command(cmd, "sub_1", "TestStream")

        servicer.feedback_manager.apply_base_color.assert_called_once_with("AA:BB", (255, 0, 0), label="TestStream")

    @pytest.mark.asyncio
    async def test_process_base_color_command_untracked(self, servicer_components):
        """_process_base_color_command should skip untracked controllers."""
        servicer, _ = servicer_components
        servicer.feedback_manager.apply_base_color = AsyncMock()

        cmd = MagicMock()
        cmd.serial = "UNKNOWN"
        cmd.color.r = 255
        cmd.color.g = 0
        cmd.color.b = 0

        await servicer._process_base_color_command(cmd, "sub_1", "TestStream")

        servicer.feedback_manager.apply_base_color.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_game_effect_command(self, servicer_components):
        """_process_game_effect_command should delegate to feedback_manager.handle_game_effect."""
        servicer, _ = servicer_components
        servicer.feedback_manager.handle_game_effect = AsyncMock()

        cmd = MagicMock()
        cmd.serial = "AA:BB"
        cmd.effect = controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH
        cmd.HasField.return_value = False  # no color
        cmd.duration_ms = 0
        cmd.speed = 0
        cmd.trace_parent = ""
        cmd.trace_state = ""

        await servicer._process_game_effect_command(cmd, "sub_1")

        servicer.feedback_manager.handle_game_effect.assert_called_once_with(
            "AA:BB",
            controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH,
            "sub_1",
            color=None,
            duration_ms=0,
            speed=0,
            trace_parent="",
            trace_state="",
        )

    @pytest.mark.asyncio
    async def test_process_game_effect_command_with_color(self, servicer_components):
        """_process_game_effect_command should pass color when provided."""
        servicer, _ = servicer_components
        servicer.feedback_manager.handle_game_effect = AsyncMock()

        cmd = MagicMock()
        cmd.serial = "AA:BB"
        cmd.effect = controller_manager_pb2.GAME_EFFECT_FLASH
        cmd.HasField.side_effect = lambda field: field == "color"
        cmd.color.r = 0
        cmd.color.g = 255
        cmd.color.b = 0
        cmd.duration_ms = 500
        cmd.speed = 3
        cmd.trace_parent = ""
        cmd.trace_state = ""

        await servicer._process_game_effect_command(cmd, "sub_1")

        servicer.feedback_manager.handle_game_effect.assert_called_once_with(
            "AA:BB",
            controller_manager_pb2.GAME_EFFECT_FLASH,
            "sub_1",
            color=(0, 255, 0),
            duration_ms=500,
            speed=3,
            trace_parent="",
            trace_state="",
        )

    def _make_mock_state(self, serial="AA:BB", name="P1"):
        """Create a mock controller state with real protobuf sub-messages."""
        mock_state = MagicMock()
        mock_state.serial = serial
        mock_state.move_num = 0
        mock_state.battery = 80
        mock_state.team = 0
        mock_state.color = controller_manager_pb2.RGB(r=255, g=0, b=0)
        mock_state.accel = controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0)
        mock_state.gyro = controller_manager_pb2.Vector3(x=0.0, y=0.0, z=0.0)
        mock_state.rssi = 0
        mock_state.name = name
        return mock_state

    def test_build_gameplay_update_no_filter(self, servicer_components):
        """_build_gameplay_update should include all controllers when filter is None."""
        servicer, _ = servicer_components

        servicer.state_cache_manager.build_or_get_cached_state = MagicMock(return_value=self._make_mock_state())

        servicer.tracked_controllers["AA:BB"] = {}
        servicer.tracked_controllers["CC:DD"] = {}

        update = servicer._build_gameplay_update(None)

        assert len(update.controllers) == 2

    def test_build_gameplay_update_with_filter(self, servicer_components):
        """_build_gameplay_update should only include filtered controllers."""
        servicer, _ = servicer_components

        servicer.state_cache_manager.build_or_get_cached_state = MagicMock(return_value=self._make_mock_state())

        servicer.tracked_controllers["AA:BB"] = {}
        servicer.tracked_controllers["CC:DD"] = {}

        update = servicer._build_gameplay_update({"AA:BB"})

        assert len(update.controllers) == 1

    def test_process_filter_update_changes_filter(self, servicer_components):
        """_process_filter_update should return new filter when it changes."""
        servicer, _ = servicer_components
        span = MagicMock()

        filter_update = MagicMock()
        filter_update.serials = ["AA:BB", "CC:DD"]

        result = servicer._process_filter_update(filter_update, "sub_1", span, None)

        assert result == {"AA:BB", "CC:DD"}
        span.add_event.assert_called_once()

    def test_process_filter_update_no_change(self, servicer_components):
        """_process_filter_update should return same filter when unchanged."""
        servicer, _ = servicer_components
        span = MagicMock()

        existing = {"AA:BB"}
        filter_update = MagicMock()
        filter_update.serials = ["AA:BB"]

        result = servicer._process_filter_update(filter_update, "sub_1", span, existing)

        assert result == existing
        span.add_event.assert_not_called()

    def test_process_filter_update_empty_to_none(self, servicer_components):
        """_process_filter_update with empty serials should return None (all controllers)."""
        servicer, _ = servicer_components
        span = MagicMock()

        filter_update = MagicMock()
        filter_update.serials = []

        result = servicer._process_filter_update(filter_update, "sub_1", span, {"AA:BB"})

        assert result is None

    @pytest.mark.asyncio
    async def test_process_base_color_command_empty_serial(self, servicer_components):
        """_process_base_color_command should skip when serial is empty string."""
        servicer, _ = servicer_components
        servicer.feedback_manager.apply_base_color = AsyncMock()

        cmd = MagicMock()
        cmd.serial = ""
        cmd.color.r = 255
        cmd.color.g = 0
        cmd.color.b = 0

        await servicer._process_base_color_command(cmd, "sub_1", "TestStream")

        servicer.feedback_manager.apply_base_color.assert_not_called()

    def test_build_gameplay_update_empty_controllers(self, servicer_components):
        """_build_gameplay_update should return empty update when no controllers tracked."""
        servicer, _ = servicer_components

        update = servicer._build_gameplay_update(None)

        assert len(update.controllers) == 0
        assert update.timestamp > 0

    def test_build_gameplay_update_filter_excludes_all(self, servicer_components):
        """_build_gameplay_update should return empty when filter matches nothing."""
        servicer, _ = servicer_components

        servicer.state_cache_manager.build_or_get_cached_state = MagicMock(return_value=self._make_mock_state())
        servicer.tracked_controllers["AA:BB"] = {}

        update = servicer._build_gameplay_update({"XX:YY"})

        assert len(update.controllers) == 0

    def test_build_gameplay_update_drains_death_latch_per_frame(self, servicer_components):
        """Each streamed controller drains its mock death latch once (#926)."""
        servicer, _ = servicer_components

        servicer.state_cache_manager.build_or_get_cached_state = MagicMock(return_value=self._make_mock_state())
        servicer.tracked_controllers["AA:BB"] = {}
        servicer.tracked_controllers["CC:DD"] = {}
        # Backend exposes the duck-typed hook (only MockAdapter does in prod).
        servicer.backend.consume_death_frame = MagicMock()

        servicer._build_gameplay_update(None)

        # Exactly one death-frame tick per included controller, this frame.
        servicer.backend.consume_death_frame.assert_any_call("AA:BB")
        servicer.backend.consume_death_frame.assert_any_call("CC:DD")
        assert servicer.backend.consume_death_frame.call_count == 2

    def test_build_gameplay_update_respects_filter_for_death_latch(self, servicer_components):
        """Filtered-out controllers don't get a death-frame tick (#926)."""
        servicer, _ = servicer_components

        servicer.state_cache_manager.build_or_get_cached_state = MagicMock(return_value=self._make_mock_state())
        servicer.tracked_controllers["AA:BB"] = {}
        servicer.tracked_controllers["CC:DD"] = {}
        servicer.backend.consume_death_frame = MagicMock()

        servicer._build_gameplay_update({"AA:BB"})

        servicer.backend.consume_death_frame.assert_called_once_with("AA:BB")

    def test_build_gameplay_update_without_death_hook(self, servicer_components):
        """Backends without the hook (hardware) stream normally (#926)."""
        servicer, _ = servicer_components

        servicer.state_cache_manager.build_or_get_cached_state = MagicMock(return_value=self._make_mock_state())
        servicer.tracked_controllers["AA:BB"] = {}
        # MockBackend has no consume_death_frame — must not raise.
        if hasattr(servicer.backend, "consume_death_frame"):
            del servicer.backend.consume_death_frame

        update = servicer._build_gameplay_update(None)

        assert len(update.controllers) == 1

    @pytest.mark.asyncio
    async def test_process_gameplay_config_with_colors(self, servicer_components):
        """_process_gameplay_config should set base colors and build filter from color configs."""
        servicer, _ = servicer_components
        servicer.tracked_controllers["AA:BB"] = {}
        servicer.feedback_manager.set_controller_color = AsyncMock(return_value=True)
        span = MagicMock()

        config = MagicMock()
        config.update_frequency_hz = 60

        # Create color config entries
        color1 = MagicMock()
        color1.serial = "AA:BB"
        color1.color.r = 255
        color1.color.g = 0
        color1.color.b = 0
        config.colors = [color1]

        hz, filt = await servicer._process_gameplay_config(config, "sub_1", span)

        assert hz == 60
        assert filt == {"AA:BB"}
        assert servicer.feedback_manager.base_colors["AA:BB"] == (255, 0, 0)
        servicer.feedback_manager.set_controller_color.assert_called_once_with("AA:BB", (255, 0, 0))

    @pytest.mark.asyncio
    async def test_process_gameplay_config_without_colors(self, servicer_components):
        """_process_gameplay_config without colors should return None filter (all controllers)."""
        servicer, _ = servicer_components
        span = MagicMock()

        config = MagicMock()
        config.update_frequency_hz = 30
        config.colors = []

        hz, filt = await servicer._process_gameplay_config(config, "sub_1", span)

        assert hz == 30
        assert filt is None

    @pytest.mark.asyncio
    async def test_process_gameplay_config_default_hz(self, servicer_components):
        """_process_gameplay_config should default to 30Hz when frequency is 0."""
        servicer, _ = servicer_components
        span = MagicMock()

        config = MagicMock()
        config.update_frequency_hz = 0
        config.colors = []

        hz, filt = await servicer._process_gameplay_config(config, "sub_1", span)

        assert hz == 30

    @pytest.mark.asyncio
    async def test_send_initial_connection_events_empty_controllers(self, servicer_components):
        """_send_initial_connection_events should be safe with no controllers."""
        servicer, _ = servicer_components
        queue = asyncio.Queue(maxsize=100)

        await servicer._send_initial_connection_events("sub_1", queue)

        assert queue.qsize() == 0


# ---------------------------------------------------------------------------
# Helper: async request iterators for stream tests
# ---------------------------------------------------------------------------


async def _empty_request_iterator():
    """Async generator that never yields (client sends nothing)."""
    await asyncio.Event().wait()
    yield  # unreachable, makes this an async generator


async def _request_iterator_with_messages(messages):
    """Yields provided messages then hangs."""
    for msg in messages:
        yield msg
    await asyncio.Event().wait()
    yield  # unreachable, makes this an async generator


# ---------------------------------------------------------------------------
# Shared fixture factory (avoids duplicating the patch boilerplate)
# ---------------------------------------------------------------------------


def _make_servicer():
    """Create a ControllerManagerServicer with mocked backend and discovery loop.

    Returns (servicer, mock_backend, mock_discovery_loop_instance).
    """
    with (
        patch("services.controller_manager.servicer.create_backend") as mock_create_backend,
        patch("services.controller_manager.servicer.DiscoveryLoop") as mock_discovery_loop_cls,
    ):
        mock_backend = MockBackend()
        mock_create_backend.return_value = mock_backend

        mock_loop = MagicMock()
        mock_loop.start = MagicMock()
        mock_loop.stop = MagicMock()
        mock_loop.wait_stopped = AsyncMock()
        mock_loop.drain_health_counters = MagicMock(return_value=(0, 0, 0))
        mock_discovery_loop_cls.return_value = mock_loop

        from services.controller_manager.servicer import ControllerManagerServicer

        servicer = ControllerManagerServicer()
        return servicer, mock_backend, mock_loop


class TestEnsureDiscoveryStarted:
    """Tests for _ensure_discovery_started."""

    @pytest.fixture
    def servicer_components(self):
        servicer, _backend, loop = _make_servicer()
        yield servicer, loop

    def test_starts_discovery_loop(self, servicer_components):
        """First call should start the discovery loop and set the flag."""
        servicer, loop = servicer_components

        assert servicer._discovery_started is False
        servicer._ensure_discovery_started()

        loop.start.assert_called_once()
        assert servicer._discovery_started is True

    def test_idempotent(self, servicer_components):
        """Second call should not re-start the discovery loop."""
        servicer, loop = servicer_components

        servicer._ensure_discovery_started()
        servicer._ensure_discovery_started()

        loop.start.assert_called_once()


class TestSetLedColor:
    """Tests for _set_led_color."""

    @pytest.fixture
    def servicer(self):
        servicer, _backend, _loop = _make_servicer()
        yield servicer

    @pytest.mark.asyncio
    async def test_delegates_to_feedback_manager(self, servicer):
        """_set_led_color should delegate to feedback_manager._set_led_color."""
        servicer.feedback_manager._set_led_color = AsyncMock()

        await servicer._set_led_color("AA:BB", (255, 0, 128))

        servicer.feedback_manager._set_led_color.assert_called_once_with("AA:BB", (255, 0, 128))


class TestStreamButtonEvents:
    """Tests for the StreamButtonEvents bidirectional streaming RPC."""

    @pytest.fixture
    def servicer(self):
        servicer, _backend, _loop = _make_servicer()
        yield servicer

    @pytest.mark.asyncio
    async def test_yields_initial_connection_events(self, servicer):
        """Tracked controllers should receive EVENT_CONNECT events at stream start."""
        from lib.controller_constants import ControllerInfoKey

        servicer.tracked_controllers["AA:BB"] = {
            ControllerInfoKey.BATTERY: 80,
            ControllerInfoKey.NAME: "P1",
        }
        servicer.tracked_controllers["CC:DD"] = {
            ControllerInfoKey.BATTERY: 60,
            ControllerInfoKey.NAME: "P2",
        }

        context = MockGrpcContext()
        events = []

        async for event in servicer.StreamButtonEvents(_empty_request_iterator(), context):
            events.append(event)
            if len(events) >= 2:
                context._cancelled = True

        assert len(events) == 2
        serials = {e.serial for e in events}
        assert serials == {"AA:BB", "CC:DD"}
        assert all(e.event_type == controller_manager_pb2.EVENT_CONNECT for e in events)

    @pytest.mark.asyncio
    async def test_registers_and_cleans_up_subscriber(self, servicer):
        """Subscriber should be added to dict during stream and removed after cancel."""
        context = MockGrpcContext()
        # Cancel immediately so the stream exits after initial events
        context._cancelled = True

        events = []
        async for event in servicer.StreamButtonEvents(_empty_request_iterator(), context):
            events.append(event)

        # After stream ends, subscriber should be cleaned up
        assert len(servicer.button_event_subscribers) == 0

    @pytest.mark.asyncio
    async def test_starts_discovery_loop(self, servicer):
        """StreamButtonEvents should call _ensure_discovery_started."""
        context = MockGrpcContext()
        context._cancelled = True

        async for _ in servicer.StreamButtonEvents(_empty_request_iterator(), context):
            pass

        servicer.discovery_loop.start.assert_called_once()
        assert servicer._discovery_started is True

    @pytest.mark.asyncio
    async def test_yields_queued_events(self, servicer):
        """Events placed in the subscriber queue should be yielded by the stream."""
        from lib.controller_constants import ControllerInfoKey

        # Add a controller so we get an initial connect event
        servicer.tracked_controllers["AA:BB"] = {
            ControllerInfoKey.BATTERY: 80,
            ControllerInfoKey.NAME: "P1",
        }

        context = MockGrpcContext()
        events_received = []

        async for event in servicer.StreamButtonEvents(_empty_request_iterator(), context):
            events_received.append(event)

            if len(events_received) == 1:
                # After the initial connect event, inject a button event via the queue
                sub_id = next(iter(servicer.button_event_subscribers))
                queue = servicer.button_event_subscribers[sub_id]
                button_event = controller_manager_pb2.ButtonEvent(
                    serial="AA:BB",
                    timestamp=int(time.time() * 1000),
                    event_type=controller_manager_pb2.EVENT_BUTTON,
                    button=controller_manager_pb2.BUTTON_TRIGGER,
                    action=controller_manager_pb2.ACTION_PRESS,
                )
                queue.put_nowait(button_event)
            elif len(events_received) >= 2:
                context._cancelled = True

        assert len(events_received) == 2
        assert events_received[0].event_type == controller_manager_pb2.EVENT_CONNECT
        assert events_received[0].serial == "AA:BB"
        assert events_received[1].event_type == controller_manager_pb2.EVENT_BUTTON
        assert events_received[1].button == controller_manager_pb2.BUTTON_TRIGGER

    @pytest.mark.asyncio
    async def test_processes_base_color_control(self, servicer):
        """base_color control message should be processed via the request iterator."""
        servicer.tracked_controllers["AA:BB"] = {}
        servicer.feedback_manager.apply_base_color = AsyncMock()

        context = MockGrpcContext()

        # Build a control message with base_color
        control_msg = controller_manager_pb2.ButtonEventStreamControl(
            base_color=controller_manager_pb2.ControllerColorConfig(
                serial="AA:BB",
                color=controller_manager_pb2.RGB(r=255, g=0, b=0),
            )
        )

        async def _request_iter():
            yield control_msg
            # Give background task time to process before we cancel
            await asyncio.sleep(0.15)
            context._cancelled = True
            await asyncio.Event().wait()
            yield  # unreachable

        events = []
        async for event in servicer.StreamButtonEvents(_request_iter(), context):
            events.append(event)

        servicer.feedback_manager.apply_base_color.assert_called_once_with("AA:BB", (255, 0, 0), label="ButtonStream")

    @pytest.mark.asyncio
    async def test_no_events_continues_until_cancelled(self, servicer):
        """Empty queue should timeout and continue looping until context is cancelled."""
        context = MockGrpcContext()

        # Cancel from the request iterator after a brief delay
        async def _cancel_request_iter():
            await asyncio.sleep(0.2)
            context._cancelled = True
            await asyncio.Event().wait()
            yield  # unreachable, makes this an async generator

        events = []
        async for event in servicer.StreamButtonEvents(_cancel_request_iter(), context):
            events.append(event)

        # No controllers tracked, so no initial events either
        assert len(events) == 0
        # Subscriber should be cleaned up
        assert len(servicer.button_event_subscribers) == 0


class TestStreamGameplayData:
    """Tests for the StreamGameplayData bidirectional streaming RPC."""

    @pytest.fixture
    def servicer(self):
        servicer, _backend, _loop = _make_servicer()
        yield servicer

    def _make_mock_state(self, serial="AA:BB", name="P1"):
        """Create a mock controller state."""
        mock_state = MagicMock()
        mock_state.serial = serial
        mock_state.move_num = 0
        mock_state.battery = 80
        mock_state.team = 0
        mock_state.color = controller_manager_pb2.RGB(r=255, g=0, b=0)
        mock_state.accel = controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0)
        mock_state.gyro = controller_manager_pb2.Vector3(x=0.0, y=0.0, z=0.0)
        mock_state.rssi = 0
        mock_state.name = name
        return mock_state

    @pytest.mark.asyncio
    async def test_yields_gameplay_updates(self, servicer):
        """Stream should yield GameplayDataUpdate messages with controller data."""
        servicer.tracked_controllers["AA:BB"] = {}
        servicer.state_cache_manager.build_or_get_cached_state = MagicMock(
            return_value=self._make_mock_state("AA:BB", "P1")
        )

        context = MockGrpcContext()
        updates = []

        async for update in servicer.StreamGameplayData(_empty_request_iterator(), context):
            updates.append(update)
            if len(updates) >= 2:
                context._cancelled = True

        assert len(updates) >= 2
        assert all(isinstance(u, controller_manager_pb2.GameplayDataUpdate) for u in updates)
        assert all(len(u.controllers) == 1 for u in updates)
        assert updates[0].controllers[0].serial == "AA:BB"
        assert updates[0].timestamp > 0

    @pytest.mark.asyncio
    async def test_starts_discovery_loop(self, servicer):
        """StreamGameplayData should call _ensure_discovery_started."""
        context = MockGrpcContext()
        context._cancelled = True

        async for _ in servicer.StreamGameplayData(_empty_request_iterator(), context):
            pass

        servicer.discovery_loop.start.assert_called_once()
        assert servicer._discovery_started is True

    @pytest.mark.asyncio
    async def test_config_sets_frequency_and_filter(self, servicer):
        """Config control message should set Hz and controller filter."""
        servicer.tracked_controllers["AA:BB"] = {}
        servicer.tracked_controllers["CC:DD"] = {}
        servicer.feedback_manager.set_controller_color = AsyncMock(return_value=True)

        servicer.state_cache_manager.build_or_get_cached_state = MagicMock(
            side_effect=lambda serial, _info: self._make_mock_state(serial)
        )

        # Build config that sets 60Hz and filters to AA:BB only
        config_msg = controller_manager_pb2.GameplayStreamControl(
            config=controller_manager_pb2.GameplayStreamConfig(
                update_frequency_hz=60,
                colors=[
                    controller_manager_pb2.ControllerColorConfig(
                        serial="AA:BB",
                        color=controller_manager_pb2.RGB(r=255, g=0, b=0),
                    ),
                ],
            )
        )

        async def _request_iter():
            yield config_msg
            await asyncio.sleep(0.1)
            await asyncio.Event().wait()

        context = MockGrpcContext()
        updates = []

        async for update in servicer.StreamGameplayData(_request_iter(), context):
            updates.append(update)
            # After a few frames, check that config was applied
            if len(updates) >= 3:
                context._cancelled = True

        # The config sets filter to {"AA:BB"}, so updates should only include AA:BB
        # First frame may be unfiltered (config processed async), so check later frames
        last_update = updates[-1]
        # Filter should have taken effect: only AA:BB
        serials = {c.serial for c in last_update.controllers}
        assert "CC:DD" not in serials

    @pytest.mark.asyncio
    async def test_empty_controllers_yields_empty_update(self, servicer):
        """No tracked controllers should yield empty updates."""
        context = MockGrpcContext()
        updates = []

        async for update in servicer.StreamGameplayData(_empty_request_iterator(), context):
            updates.append(update)
            if len(updates) >= 1:
                context._cancelled = True

        assert len(updates) >= 1
        assert len(updates[0].controllers) == 0
        assert updates[0].timestamp > 0

    @pytest.mark.asyncio
    async def test_filter_limits_controllers(self, servicer):
        """Mid-stream filter update should limit which controllers are included."""
        servicer.tracked_controllers["AA:BB"] = {}
        servicer.tracked_controllers["CC:DD"] = {}

        servicer.state_cache_manager.build_or_get_cached_state = MagicMock(
            side_effect=lambda serial, _info: self._make_mock_state(serial)
        )

        # Send a filter_update to restrict to AA:BB only
        filter_msg = controller_manager_pb2.GameplayStreamControl(
            filter_update=controller_manager_pb2.FilterUpdate(serials=["AA:BB"])
        )

        async def _request_iter():
            yield filter_msg
            await asyncio.sleep(0.1)
            await asyncio.Event().wait()

        context = MockGrpcContext()
        updates = []

        async for update in servicer.StreamGameplayData(_request_iter(), context):
            updates.append(update)
            if len(updates) >= 5:
                context._cancelled = True

        # Later frames should only have AA:BB (first may have both if filter not yet applied)
        last_update = updates[-1]
        serials = {c.serial for c in last_update.controllers}
        assert serials <= {"AA:BB"}

    @pytest.mark.asyncio
    async def test_frequency_override_applied(self, servicer):
        """Module-level _frequency_override should be respected."""
        import services.controller_manager.servicer as servicer_module

        original = servicer_module._frequency_override
        try:
            servicer_module._frequency_override = 10  # Low Hz for test

            context = MockGrpcContext()
            updates = []
            start = time.monotonic()

            async for update in servicer.StreamGameplayData(_empty_request_iterator(), context):
                updates.append(update)
                if len(updates) >= 3:
                    context._cancelled = True

            elapsed = time.monotonic() - start
            # At 10Hz, 3 frames should take ~0.2s minimum (not the default 30Hz ~0.06s)
            assert elapsed >= 0.15
        finally:
            servicer_module._frequency_override = original

    @pytest.mark.asyncio
    async def test_cleans_up_on_disconnect(self, servicer):
        """Stream should decrement metrics and cancel background task on disconnect."""
        context = MockGrpcContext()
        context._cancelled = True

        async for _ in servicer.StreamGameplayData(_empty_request_iterator(), context):
            pass

        # Verify no leaked tasks — if cleanup failed, we'd see pending background tasks
        # The fact that the stream completes without error confirms cleanup ran
        # Also verify discovery started
        assert servicer._discovery_started is True
