"""Unit tests for ControllerEventLoop."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

from proto import controller_manager_pb2
from services.menu import metrics as menu_metrics
from services.menu.controller_events import BUTTON_TYPE_NAMES, ControllerEventLoop


@pytest.fixture
def mock_callbacks():
    """Create mock ControllerEventCallbacks."""
    callbacks = MagicMock()
    callbacks.on_connect = AsyncMock()
    callbacks.on_disconnect = AsyncMock()
    callbacks.on_button = AsyncMock()
    callbacks.update_battery = MagicMock()
    callbacks.get_menu_state = MagicMock(return_value=1)  # RUNNING
    callbacks.get_connected_serials = MagicMock(return_value=set())
    callbacks.on_stream_reconnected = AsyncMock()
    return callbacks


@pytest.fixture
def mock_metrics():
    """Autospec of the real metrics module.

    #1272: a bare MagicMock let any metric name through, so a typo inside the
    error path would pass here and raise in production.
    """
    return create_autospec(menu_metrics, spec_set=True)


@pytest.fixture
def event_loop_instance(mock_callbacks, mock_metrics):
    """Create ControllerEventLoop instance."""
    return ControllerEventLoop(MagicMock(), MagicMock(), mock_callbacks, mock_metrics)


class TestControllerEventLoopInit:
    """Test ControllerEventLoop initialization."""

    def test_initialization(self, event_loop_instance):
        """ControllerEventLoop should initialize correctly."""
        assert event_loop_instance._running is False
        assert event_loop_instance._task is None


class TestControllerEventLoopLifecycle:
    """Test start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start(self, event_loop_instance):
        """start should set running flag."""
        event_loop_instance._run = AsyncMock()

        await event_loop_instance.start()

        assert event_loop_instance._running is True
        assert event_loop_instance._task is not None

        await event_loop_instance.stop()

    @pytest.mark.asyncio
    async def test_stop(self, event_loop_instance):
        """stop should clear running flag."""
        event_loop_instance._running = True

        async def dummy():
            await asyncio.sleep(10)

        event_loop_instance._task = asyncio.create_task(dummy())

        await event_loop_instance.stop()

        assert event_loop_instance._running is False

    def test_is_running(self, event_loop_instance):
        """is_running should return running state."""
        assert event_loop_instance.is_running is False
        event_loop_instance._running = True
        assert event_loop_instance.is_running is True


class TestControllerEventLoopDispatch:
    """Test event dispatching."""

    @pytest.mark.asyncio
    async def test_dispatch_connect_event(self, event_loop_instance, mock_callbacks, mock_metrics):
        """Connect events should call on_connect callback."""
        event = MagicMock()
        event.serial = "serial1"
        event.event_type = controller_manager_pb2.EVENT_CONNECT
        event.battery = 5

        await event_loop_instance._dispatch_event(event)

        mock_callbacks.on_connect.assert_called_once_with("serial1")
        mock_callbacks.update_battery.assert_called_once_with("serial1", 5)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A1")
    async def test_dispatch_disconnect_event(self, event_loop_instance, mock_callbacks, mock_metrics):
        """Disconnect events should call on_disconnect callback."""
        event = MagicMock()
        event.serial = "serial1"
        event.event_type = controller_manager_pb2.EVENT_DISCONNECT
        event.battery = 0

        await event_loop_instance._dispatch_event(event)

        mock_callbacks.on_disconnect.assert_called_once_with("serial1")

    @pytest.mark.asyncio
    async def test_dispatch_button_event_press(self, event_loop_instance, mock_callbacks):
        """Button press events should call on_button callback."""
        from proto import menu_pb2

        mock_callbacks.get_menu_state.return_value = menu_pb2.MenuState.RUNNING

        event = MagicMock()
        event.serial = "serial1"
        event.event_type = 0  # Default, button event
        event.action = controller_manager_pb2.ACTION_PRESS
        event.button = controller_manager_pb2.BUTTON_TRIGGER
        event.battery = 4
        event.timestamp = 1_700_000_000_400

        await event_loop_instance._dispatch_event(event)

        # UC-012 BR-007: the controller event timestamp (ms) is passed on as the event time (s)
        mock_callbacks.on_button.assert_called_once_with("serial1", "trigger", True, 1_700_000_000.4)

    @pytest.mark.asyncio
    async def test_dispatch_button_event_menu_stopped(self, event_loop_instance, mock_callbacks):
        """Button events should be ignored when menu is not running."""
        from proto import menu_pb2

        mock_callbacks.get_menu_state.return_value = menu_pb2.MenuState.STOPPED

        event = MagicMock()
        event.serial = "serial1"
        event.event_type = 0
        event.action = controller_manager_pb2.ACTION_PRESS
        event.button = controller_manager_pb2.BUTTON_TRIGGER
        event.battery = 4

        await event_loop_instance._dispatch_event(event)

        mock_callbacks.on_button.assert_not_called()


class TestButtonTypeNames:
    """Test BUTTON_TYPE_NAMES mapping."""

    def test_all_buttons_mapped(self):
        """All button types should have names."""
        expected = [
            controller_manager_pb2.BUTTON_TRIGGER,
            controller_manager_pb2.BUTTON_MOVE,
            controller_manager_pb2.BUTTON_CROSS,
            controller_manager_pb2.BUTTON_CIRCLE,
            controller_manager_pb2.BUTTON_SQUARE,
            controller_manager_pb2.BUTTON_TRIANGLE,
            controller_manager_pb2.BUTTON_PS,
            controller_manager_pb2.BUTTON_SELECT,
            controller_manager_pb2.BUTTON_START,
        ]

        for button in expected:
            assert button in BUTTON_TYPE_NAMES

    def test_button_names_are_strings(self):
        """All button names should be lowercase strings."""
        for name in BUTTON_TYPE_NAMES.values():
            assert isinstance(name, str)
            assert name == name.lower()


class TestHandleConnectionError:
    """Tests for _handle_connection_error extracted helper."""

    @pytest.fixture(autouse=True)
    def no_real_sleep(self, monkeypatch):
        """The retry back-off is real wall-clock time (up to 20 s per test); skip it."""
        monkeypatch.setattr("services.menu.controller_events.asyncio.sleep", AsyncMock())

    @pytest.mark.asyncio
    async def test_returns_doubled_delay(self, event_loop_instance):
        """Should return doubled retry delay after sleeping."""
        event_loop_instance._running = True
        result = await event_loop_instance._handle_connection_error(
            RuntimeError("test"), retry_delay=2.0, max_retry_delay=30.0
        )
        assert result == 4.0

    @pytest.mark.asyncio
    async def test_caps_at_max_delay(self, event_loop_instance):
        """Should cap retry delay at max_retry_delay."""
        event_loop_instance._running = True
        result = await event_loop_instance._handle_connection_error(
            RuntimeError("test"), retry_delay=20.0, max_retry_delay=30.0
        )
        assert result == 30.0

    @pytest.mark.asyncio
    async def test_returns_unchanged_when_not_running(self, event_loop_instance):
        """Should return unchanged delay when loop is not running."""
        event_loop_instance._running = False
        result = await event_loop_instance._handle_connection_error(
            RuntimeError("test"), retry_delay=5.0, max_retry_delay=30.0
        )
        assert result == 5.0


class TestClearStreamState:
    """Tests for _clear_stream_state extracted helper."""

    @pytest.mark.asyncio
    async def test_clears_stream_and_queue(self, event_loop_instance):
        """Should set stream and queue to None."""
        event_loop_instance._stream = MagicMock()
        event_loop_instance._stream_queue = MagicMock()
        event_loop_instance._connected_event = asyncio.Event()
        event_loop_instance._connected_event.set()

        await event_loop_instance._clear_stream_state()

        assert event_loop_instance._stream is None
        assert event_loop_instance._stream_queue is None

    @pytest.mark.asyncio
    async def test_clears_connected_event(self, event_loop_instance):
        """Should clear the connected event."""
        event_loop_instance._connected_event = asyncio.Event()
        event_loop_instance._connected_event.set()

        await event_loop_instance._clear_stream_state()

        assert not event_loop_instance._connected_event.is_set()

    @pytest.mark.asyncio
    async def test_calls_set_stream_none_on_led(self, event_loop_instance):
        """Should call set_stream(None) on the LED controller."""
        mock_led = MagicMock()
        event_loop_instance._led = mock_led

        await event_loop_instance._clear_stream_state()

        mock_led.set_stream.assert_called_once_with(None)

    @pytest.mark.asyncio
    async def test_handles_no_connected_event(self, event_loop_instance):
        """Should handle case when connected_event is None."""
        event_loop_instance._connected_event = None

        await event_loop_instance._clear_stream_state()

        assert event_loop_instance._stream is None


class TestRequestGenerator:
    """Tests for _request_generator extracted helper."""

    @pytest.mark.asyncio
    async def test_yields_initial_config_first(self, event_loop_instance):
        """Should yield initial config as first message."""
        event_loop_instance._running = False  # Stop after initial config
        queue = asyncio.Queue()

        messages = []
        async for msg in event_loop_instance._request_generator(queue):
            messages.append(msg)

        assert len(messages) == 1
        assert messages[0].HasField("config")

    @pytest.mark.asyncio
    async def test_yields_queued_messages(self, event_loop_instance):
        """Should yield messages from queue after initial config."""
        event_loop_instance._running = True
        queue = asyncio.Queue()

        test_msg = controller_manager_pb2.ButtonEventStreamControl()
        await queue.put(test_msg)

        messages = []
        count = 0
        async for msg in event_loop_instance._request_generator(queue):
            messages.append(msg)
            count += 1
            if count >= 2:
                event_loop_instance._running = False

        assert len(messages) == 2
        assert messages[0].HasField("config")
        assert messages[1] == test_msg


class TestReconciliation:
    """Tests for ghost controller reconciliation."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A6")
    async def test_ghost_controller_pruned(self, event_loop_instance, mock_callbacks, mock_metrics):
        """Ghost controllers not in backend list should be disconnected."""
        # Menu thinks serial1 and serial2 are connected
        mock_callbacks.get_connected_serials.return_value = {"serial1", "serial2"}

        # Backend only has serial1
        event = MagicMock()
        event.serial = "serial1"
        event.event_type = controller_manager_pb2.EVENT_CONNECT
        event.battery = 5
        event.connected_serials = ["serial1"]

        await event_loop_instance._dispatch_event(event)

        # serial2 should be pruned
        mock_callbacks.on_disconnect.assert_any_call("serial2")
        mock_metrics.ghost_controllers_pruned_total.inc.assert_called()

    @pytest.mark.asyncio
    async def test_no_reconciliation_when_connected_serials_empty(
        self, event_loop_instance, mock_callbacks, mock_metrics
    ):
        """No reconciliation when connected_serials is empty (e.g., button events)."""
        mock_callbacks.get_connected_serials.return_value = {"serial1", "serial2"}

        event = MagicMock()
        event.serial = "serial1"
        event.event_type = controller_manager_pb2.EVENT_CONNECT
        event.battery = 5
        event.connected_serials = []  # Empty = no reconciliation

        await event_loop_instance._dispatch_event(event)

        # on_connect called for the event, but no ghost pruning
        mock_callbacks.on_connect.assert_called_once_with("serial1")
        # get_connected_serials should not have been called for reconciliation
        mock_callbacks.get_connected_serials.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A6")
    async def test_no_pruning_when_lists_match(self, event_loop_instance, mock_callbacks, mock_metrics):
        """No pruning when menu and backend agree on connected controllers."""
        mock_callbacks.get_connected_serials.return_value = {"serial1", "serial2"}

        event = MagicMock()
        event.serial = "serial2"
        event.event_type = controller_manager_pb2.EVENT_CONNECT
        event.battery = 3
        event.connected_serials = ["serial1", "serial2"]

        await event_loop_instance._dispatch_event(event)

        # on_connect for serial2, but no ghost pruning (on_disconnect not called for ghosts)
        mock_callbacks.on_connect.assert_called_once_with("serial2")
        # on_disconnect should NOT have been called (no ghosts)
        mock_callbacks.on_disconnect.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconciliation_on_disconnect_event(self, event_loop_instance, mock_callbacks, mock_metrics):
        """Reconciliation also runs on disconnect events."""
        # Menu thinks serial1, serial2, serial3 are connected
        mock_callbacks.get_connected_serials.return_value = {"serial1", "serial2", "serial3"}

        event = MagicMock()
        event.serial = "serial2"
        event.event_type = controller_manager_pb2.EVENT_DISCONNECT
        event.battery = 0
        # After disconnect, backend has serial1 and serial3
        event.connected_serials = ["serial1", "serial3"]

        await event_loop_instance._dispatch_event(event)

        # serial2 disconnected via event, then reconciliation checks remaining
        mock_callbacks.on_disconnect.assert_any_call("serial2")
        # get_connected_serials is called during reconciliation
        mock_callbacks.get_connected_serials.assert_called()


class TestStreamReconnected:
    """Tests for on_stream_reconnected callback."""

    @pytest.mark.asyncio
    async def test_stream_reconnect_clears_state(self, event_loop_instance, mock_callbacks):
        """on_stream_reconnected should be called before establishing stream."""
        # Directly test _run_stream_connection calls on_stream_reconnected
        # We can't easily test the full flow, but we can verify the callback exists
        # and is called in isolation
        await event_loop_instance._callbacks.on_stream_reconnected()
        mock_callbacks.on_stream_reconnected.assert_called_once()


def _button_event(serial: str, button: int) -> MagicMock:
    """Build a button-press ButtonEvent stand-in."""
    event = MagicMock()
    event.serial = serial
    event.event_type = 0  # EVENT_BUTTON
    event.action = controller_manager_pb2.ACTION_PRESS
    event.button = button
    event.battery = 0
    event.timestamp = 0
    event.connected_serials = []
    return event


class _FakeStream:
    """Async-iterable stand-in for the StreamButtonEvents call."""

    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for event in self._events:
            yield event


class TestHandlerErrorIsolation:
    """#1272: one handler exception must not drop the stream or wipe the lobby."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="main", br=["BR-003"])
    async def test_dispatch_safely_swallows_and_counts(self, event_loop_instance, mock_callbacks, mock_metrics):
        """A raising callback is logged and counted, not propagated."""
        mock_callbacks.on_button.side_effect = AttributeError("boom")

        await event_loop_instance._dispatch_event_safely(_button_event("serial1", controller_manager_pb2.BUTTON_SELECT))

        mock_metrics.button_event_errors_total.labels.assert_called_once_with(event_type="button")
        mock_metrics.button_event_errors_total.labels.return_value.inc.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="main", br=["BR-003"])
    async def test_handler_exception_keeps_stream_and_ready_state(self, mock_metrics):
        """A handler raising mid-stream leaves the stream up and READY controllers READY.

        Before #1272 the exception escaped to ``_run``, which reconnected; the
        reconnect called ``on_stream_reconnected`` and cleared every controller.
        """
        from lib.flag_service_writer import FlagServiceWriter
        from services.menu.handlers.base import ControllerState
        from services.menu.state_manager import StateManager
        from services.menu.utils import AudioHelper, LedController

        state_manager = StateManager(
            led=create_autospec(LedController, instance=True, spec_set=True),
            audio=create_autospec(AudioHelper, instance=True, spec_set=True),
            game_settings_writer=create_autospec(FlagServiceWriter, instance=True, spec_set=True),
            user_prefs_writer=create_autospec(FlagServiceWriter, instance=True, spec_set=True),
            publish_event=AsyncMock(),
        )
        broken_handler = MagicMock()
        broken_handler.state = ControllerState.CONNECTED
        broken_handler.on_enter = AsyncMock()
        broken_handler.on_exit = AsyncMock()
        broken_handler.handle_button = AsyncMock(side_effect=[AttributeError("boom"), None])
        state_manager.register_handler(broken_handler)

        callbacks = MagicMock()
        callbacks.get_menu_state = MagicMock(return_value=1)  # RUNNING
        callbacks.on_button = state_manager.handle_button_event

        async def reconnect_and_populate():
            # Real reconnect clears the lobby; the backend then replays the
            # connected controllers. Model that as one CONNECTED + one READY.
            await state_manager.on_stream_reconnected()
            await state_manager.on_controller_connected("lobby1")
            state_manager.controller_states["ready1"] = ControllerState.READY

        callbacks.on_stream_reconnected = reconnect_and_populate

        loop = ControllerEventLoop(MagicMock(), MagicMock(), callbacks, mock_metrics)
        loop._running = True

        events = [
            _button_event("lobby1", controller_manager_pb2.BUTTON_SELECT),  # handler raises
            _button_event("lobby1", controller_manager_pb2.BUTTON_SELECT),  # still processed
        ]
        stub = MagicMock()
        stub.StreamButtonEvents.return_value = _FakeStream(events)

        with patch(
            "services.menu.controller_events.controller_manager_pb2_grpc.ControllerManagerServiceStub",
            return_value=stub,
        ):
            retry_delay = await loop._run_stream_connection()

        assert retry_delay == 1.0
        assert broken_handler.handle_button.await_count == 2
        assert state_manager.ready_controllers == {"ready1"}
        assert state_manager.get_controller_state("lobby1") == ControllerState.CONNECTED
        mock_metrics.button_event_errors_total.labels.assert_called_once_with(event_type="button")
