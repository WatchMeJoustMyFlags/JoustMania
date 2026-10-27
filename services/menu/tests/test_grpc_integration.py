"""
In-process gRPC integration tests for MenuServicer.

Spins up a real grpc.aio.server() on an ephemeral port, connects with
real stubs, and tests RPCs through the actual gRPC wire protocol.
External dependencies (flagd, gRPC channels to other services) are mocked.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import grpc.aio
import pytest
import pytest_asyncio

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from proto import menu_pb2, menu_pb2_grpc


def _make_flag_client_mock():
    """Create a mock OpenFeature flag client with sensible defaults."""
    mock = MagicMock()
    mock.get_string_value.return_value = "gen_female"
    mock.get_integer_value.return_value = 2
    mock.get_boolean_value.return_value = True
    mock.get_float_value.return_value = 4.0
    return mock


@pytest_asyncio.fixture
async def grpc_service():
    """
    Start a real gRPC server with MenuServicer, mocking external deps.

    Mocks flagd, gRPC channels to other services, and hardware utilities.
    Yields (servicer, stub) for direct servicer access and gRPC client calls.
    """
    mock_channel = MagicMock()
    mock_channel.close = AsyncMock()

    with (
        patch("lib.grpc_utils.create_channel", return_value=mock_channel),
        patch("lib.feature_flags.init_flag_domain"),
        patch("lib.feature_flags.get_flag_client", return_value=_make_flag_client_mock()),
        patch("lib.feature_flags.set_game_transaction_context"),
        patch("lib.flag_service_writer.FlagServiceWriter"),
        patch("services.menu.servicer.LedController"),
        patch("services.menu.servicer.AudioHelper") as mock_audio_cls,
        patch("services.menu.servicer.ControllerEventLoop") as mock_events_cls,
    ):
        # Configure AudioHelper mock
        mock_audio = AsyncMock()
        mock_audio_cls.return_value = mock_audio

        # Configure ControllerEventLoop mock
        mock_events = MagicMock()
        mock_events.start = AsyncMock()
        mock_events.stop = AsyncMock()
        mock_events.is_running = False
        mock_events.wait_for_connection = AsyncMock()
        mock_events_cls.return_value = mock_events

        from services.menu.servicer import MenuServicer

        servicer = MenuServicer()

    server = grpc.aio.server()
    menu_pb2_grpc.add_MenuServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("[::]:0")
    await server.start()

    channel = grpc.aio.insecure_channel(f"localhost:{port}")
    stub = menu_pb2_grpc.MenuServiceStub(channel)

    yield servicer, stub

    await channel.close()
    await server.stop(grace=0)


async def _wait_for_subscribers(servicer, count=1, max_wait=2.0):
    """Wait until the event publisher has at least `count` subscribers."""
    deadline = asyncio.get_event_loop().time() + max_wait
    while len(servicer.event_publisher._subscribers) < count:
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"Timed out waiting for {count} subscriber(s)")
        await asyncio.sleep(0.01)


def _slow_down_subscribe(publisher, delay=0.2):
    """Make ``publisher.subscribe`` slow, so headers sent before it completes are caught."""
    real_subscribe = publisher.subscribe

    async def slow_subscribe(*args, **kwargs):
        await asyncio.sleep(delay)
        return await real_subscribe(*args, **kwargs)

    publisher.subscribe = slow_subscribe


class _HeaderSendFails:
    """Servicer context of a client that disconnects while the headers are sent."""

    def cancelled(self):
        return False

    async def send_initial_metadata(self, _metadata):
        raise asyncio.CancelledError


# ---------------------------------------------------------------------------
# Unary RPCs
# ---------------------------------------------------------------------------


class TestStartMenu:
    """StartMenu through real gRPC wire protocol."""

    @pytest.mark.asyncio
    async def test_start_menu_success(self, grpc_service):
        """StartMenu transitions to RUNNING state."""
        servicer, stub = grpc_service

        response = await stub.StartMenu(menu_pb2.StartMenuRequest())

        assert response.success is True
        assert response.error == ""
        assert servicer.state == menu_pb2.MenuState.RUNNING

    @pytest.mark.asyncio
    async def test_start_menu_already_running(self, grpc_service):
        """StartMenu returns error when already running."""
        servicer, stub = grpc_service
        servicer.state = menu_pb2.MenuState.RUNNING

        response = await stub.StartMenu(menu_pb2.StartMenuRequest())

        assert response.success is False
        assert "already running" in response.error.lower()


class TestStopMenu:
    """StopMenu through real gRPC wire protocol."""

    @pytest.mark.asyncio
    async def test_stop_menu_success(self, grpc_service):
        """StopMenu transitions from RUNNING to STOPPED."""
        servicer, stub = grpc_service
        servicer.state = menu_pb2.MenuState.RUNNING

        response = await stub.StopMenu(menu_pb2.StopMenuRequest())

        assert response.success is True
        assert response.error == ""
        assert servicer.state == menu_pb2.MenuState.STOPPED

    @pytest.mark.asyncio
    async def test_stop_menu_already_stopped(self, grpc_service):
        """StopMenu returns error when already stopped."""
        _servicer, stub = grpc_service

        response = await stub.StopMenu(menu_pb2.StopMenuRequest())

        assert response.success is False
        assert "already stopped" in response.error.lower()


class TestProcessInput:
    """ProcessInput through real gRPC wire protocol."""

    @pytest.mark.asyncio
    async def test_process_input_success(self, grpc_service):
        """ProcessInput succeeds with valid input type."""
        _servicer, stub = grpc_service

        response = await stub.ProcessInput(
            menu_pb2.ProcessInputRequest(input_type="web_command", data={"command": "select_game", "game_name": "FFA"})
        )

        assert response.success is True
        assert response.error == ""

    @pytest.mark.asyncio
    async def test_process_input_unknown_type(self, grpc_service):
        """ProcessInput ignores unknown input types gracefully."""
        _servicer, stub = grpc_service

        response = await stub.ProcessInput(menu_pb2.ProcessInputRequest(input_type="unknown_type", data={}))

        assert response.success is True


# ---------------------------------------------------------------------------
# StreamMenuEvents
# ---------------------------------------------------------------------------


class TestStreamMenuEvents:
    """StreamMenuEvents through real gRPC wire protocol."""

    @pytest.mark.asyncio
    async def test_stream_receives_events(self, grpc_service):
        """Events published via event_publisher arrive through gRPC stream."""
        servicer, stub = grpc_service

        received = []

        async def consume():
            stream = stub.StreamMenuEvents(menu_pb2.StreamMenuEventsRequest())
            async for event in stream:
                received.append(event)
                if len(received) >= 2:
                    stream.cancel()
                    break

        task = asyncio.create_task(consume())
        await _wait_for_subscribers(servicer)

        await servicer.event_publisher.publish("selection_changed", {"game_name": "FFA"})
        await servicer.event_publisher.publish("menu_started", {})

        await asyncio.wait_for(task, timeout=5.0)

        assert len(received) == 2
        assert received[0].event_type == "selection_changed"
        assert received[0].data["game_name"] == "FFA"
        assert received[1].event_type == "menu_started"

    @pytest.mark.asyncio
    async def test_headers_mark_the_subscription(self, grpc_service):
        """Headers arrive once the subscriber is registered, before any event (#1613).

        A client that awaits ``initial_metadata()`` and then acts must see the
        event of that action: no polling of the server's subscriber list.
        """
        servicer, stub = grpc_service
        _slow_down_subscribe(servicer.event_publisher)

        stream = stub.StreamMenuEvents(menu_pb2.StreamMenuEventsRequest())
        await asyncio.wait_for(stream.initial_metadata(), timeout=2.0)
        # subscribe() is slow: headers sent before it completes fail here.
        assert len(servicer.event_publisher._subscribers) == 1

        await servicer.event_publisher.publish("selection_changed", {"game_name": "FFA"})
        event = await asyncio.wait_for(stream.read(), timeout=2.0)
        stream.cancel()

        assert event.event_type == "selection_changed"

    @pytest.mark.asyncio
    async def test_client_leaving_during_header_send_unsubscribes(self, grpc_service):
        """A client gone while the headers are sent leaves no subscriber and no open-stream gauge (#1613)."""
        servicer, _stub = grpc_service

        with (
            patch("services.menu.servicer.metrics.stream_connections_active") as gauge,
            pytest.raises(asyncio.CancelledError),
        ):
            async for _ in servicer.StreamMenuEvents(menu_pb2.StreamMenuEventsRequest(), _HeaderSendFails()):
                pass

        assert len(servicer.event_publisher._subscribers) == 0
        assert gauge.inc.call_count == gauge.dec.call_count == 1

    @pytest.mark.asyncio
    async def test_stream_client_disconnect_cleanup(self, grpc_service):
        """Cancelling stream triggers unsubscribe cleanup."""
        servicer, stub = grpc_service

        stream = stub.StreamMenuEvents(menu_pb2.StreamMenuEventsRequest())
        await _wait_for_subscribers(servicer)
        assert len(servicer.event_publisher._subscribers) == 1

        stream.cancel()
        await asyncio.sleep(0.3)

        assert len(servicer.event_publisher._subscribers) == 0
