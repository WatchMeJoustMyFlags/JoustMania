"""
In-process gRPC integration tests for GameCoordinatorServicer.

Spins up a real grpc.aio.server() on an ephemeral port, connects with
real stubs, and tests RPCs through the actual gRPC wire protocol.
This validates serialization, streaming, error codes, and metadata
— things that unit tests with mocked contexts miss.

Only external dependencies (ControllerManager, Audio, GameFactory) are mocked.
The servicer, EventBus, gRPC server, and streaming protocol are all real.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import grpc
import grpc.aio
import pytest
import pytest_asyncio

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from lib.types import GameEvent
from proto import controller_manager_pb2, controller_manager_pb2_grpc, game_coordinator_pb2, game_coordinator_pb2_grpc
from services.game_coordinator import game_session as game_session_mod
from services.game_coordinator.servicer import GameCoordinatorServicer

# #775: the game-loop thread moved onto GameSession. Patch its start_thread to
# keep the real background thread from spinning while still registering the
# session and publishing game_start on the (primary) bus.
_NO_THREAD = patch.object(game_session_mod.GameSession, "start_thread", lambda _self: None)


@pytest_asyncio.fixture
async def grpc_service():
    """
    Start a real gRPC server on an ephemeral port with the GameCoordinatorServicer.

    Yields (servicer, stub) for direct servicer access and gRPC client calls.
    """
    server = grpc.aio.server()
    servicer = GameCoordinatorServicer()
    game_coordinator_pb2_grpc.add_GameCoordinatorServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("[::]:0")
    await server.start()

    channel = grpc.aio.insecure_channel(f"localhost:{port}")
    stub = game_coordinator_pb2_grpc.GameCoordinatorServiceStub(channel)

    yield servicer, stub

    await channel.close()
    await server.stop(grace=0)


async def _wait_for_subscribers(servicer, count=1, max_wait=2.0):
    """Wait until the event bus has at least `count` subscribers."""
    deadline = asyncio.get_event_loop().time() + max_wait
    while servicer.event_bus.subscriber_count < count:
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"Timed out waiting for {count} subscriber(s)")
        await asyncio.sleep(0.01)


async def _wait_for_received(received, count=1, max_wait=2.0):
    """Wait until the consumer collected ``count`` events (a start stream's game_start, #1783)."""
    deadline = asyncio.get_event_loop().time() + max_wait
    while len(received) < count:
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"Timed out waiting for {count} event(s)")
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

    def invocation_metadata(self):
        return []

    async def send_initial_metadata(self, _metadata):
        raise asyncio.CancelledError


def _make_start_config(game_name="FFA", num_players=3, sensitivity=2, origin=None):
    """Build a StartGameConfig with the given parameters.

    Defaults to MENU origin (#837): these tests assert the legacy single primary
    game behavior, now reserved for the real (menu-origin) game.
    """
    if origin is None:
        origin = game_coordinator_pb2.GAME_ORIGIN_MENU
    return game_coordinator_pb2.StartGameConfig(
        game_name=game_name,
        players=[game_coordinator_pb2.Player(serial=f"p{i}") for i in range(num_players)],
        sensitivity=sensitivity,
        origin=origin,
    )


class _MockSpanContext:
    """Minimal SpanContext: the servicer reads trace_id off the inbound span's
    context to decide whether to add a #1157 game-trace Link. trace_id=0 (invalid,
    as for an unsampled span) means no Link is attempted — the path direct
    _start_game_from_config callers exercise."""

    trace_id = 0
    span_id = 0


class _MockSpan:
    """Minimal span for direct _start_game_from_config calls (no gRPC span)."""

    def set_attribute(self, key, value):
        pass

    def add_event(self, name, attributes=None):
        pass

    def get_span_context(self):
        return _MockSpanContext()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# ---------------------------------------------------------------------------
# Unary RPCs
# ---------------------------------------------------------------------------


class TestGetGameState:
    """GetGameState through real gRPC wire protocol."""

    @pytest.mark.asyncio
    async def test_get_game_state_idle(self, grpc_service):
        """GetGameState returns IDLE state when no game running."""
        _servicer, stub = grpc_service

        response = await stub.GetGameState(game_coordinator_pb2.GetGameStateRequest())

        assert response.success is True
        assert response.game_info.state == game_coordinator_pb2.GameState.IDLE
        assert response.game_info.game_mode == ""
        assert response.game_info.game_id == ""


class TestForceEndGame:
    """ForceEndGame through real gRPC wire protocol."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-027", flow="A2")
    async def test_force_end_game_no_game(self, grpc_service):
        """ForceEndGame returns error when no game in progress."""
        _servicer, stub = grpc_service

        response = await stub.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason="test"))

        assert response.success is False
        assert "no game in progress" in response.error.lower()

    @pytest.mark.asyncio
    async def test_force_end_game_during_game(self, grpc_service):
        """ForceEndGame stops a running game and returns success."""
        servicer, stub = grpc_service

        # Start a primary game (no real thread), advance it to RUNNING, and
        # attach a mock live game so force_end() is invoked.
        with _NO_THREAD:
            config = _make_start_config("FFA", num_players=3)
            _, game_id = await servicer._start_game_from_config(config, _MockSpan())
            await servicer.event_bus.publish(GameEvent.GAME_STARTED, {"game_id": game_id})

        mock_game = MagicMock()
        servicer.sessions[game_id].current_game = mock_game

        response = await stub.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason="user requested"))

        assert response.success is True
        assert response.error == ""
        mock_game.force_end.assert_called_once()

        # After force-end the primary slot frees -> GetGameState reports IDLE.
        state_resp = await stub.GetGameState(game_coordinator_pb2.GetGameStateRequest())
        assert state_resp.game_info.state == game_coordinator_pb2.GameState.IDLE


# ---------------------------------------------------------------------------
# StreamGameEvents — event subscription (no game start)
# ---------------------------------------------------------------------------


class TestStreamSubscription:
    """StreamGameEvents subscription and event delivery through gRPC."""

    @pytest.mark.asyncio
    async def test_stream_receives_published_events(self, grpc_service):
        """Subscribe (no start_config), publish via event_bus, verify events arrive."""
        servicer, stub = grpc_service

        request = game_coordinator_pb2.StreamEventsRequest()
        received = []

        async def consume():
            stream = stub.StreamGameEvents(request)
            async for event in stream:
                received.append(event)
                if len(received) >= 2:
                    stream.cancel()
                    break

        task = asyncio.create_task(consume())
        await _wait_for_subscribers(servicer)

        await servicer.event_bus.publish("test_event_1", {"key": "value1"})
        await servicer.event_bus.publish("test_event_2", {"key": "value2"})

        await asyncio.wait_for(task, timeout=5.0)

        assert len(received) == 2
        assert received[0].event_type == "test_event_1"
        assert received[1].event_type == "test_event_2"

    @pytest.mark.asyncio
    async def test_headers_mark_the_subscription(self, grpc_service):
        """Headers arrive once the subscriber is registered, before any event (#1613).

        A client that awaits ``initial_metadata()`` and then acts must see the
        event of that action: no polling of the server's subscriber list.
        """
        servicer, stub = grpc_service
        _slow_down_subscribe(servicer.event_bus)

        stream = stub.StreamGameEvents(game_coordinator_pb2.StreamEventsRequest())
        await asyncio.wait_for(stream.initial_metadata(), timeout=2.0)
        # subscribe() is slow: headers sent before it completes fail here.
        assert servicer.event_bus.subscriber_count == 1

        await servicer.event_bus.publish("test_event_1", {"key": "value1"})
        event = await asyncio.wait_for(stream.read(), timeout=2.0)
        stream.cancel()

        assert event.event_type == "test_event_1"

    @pytest.mark.asyncio
    async def test_client_leaving_during_header_send_unsubscribes(self, grpc_service):
        """A client gone while the headers are sent leaves no subscriber behind (#1613)."""
        servicer, _stub = grpc_service

        with pytest.raises(asyncio.CancelledError):
            async for _ in servicer.StreamGameEvents(game_coordinator_pb2.StreamEventsRequest(), _HeaderSendFails()):
                pass

        assert servicer.event_bus.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_stream_multiple_subscribers(self, grpc_service):
        """Two concurrent streams both receive the same events."""
        servicer, stub = grpc_service

        request = game_coordinator_pb2.StreamEventsRequest()
        received_a = []
        received_b = []

        async def consume(target_list):
            stream = stub.StreamGameEvents(request)
            async for event in stream:
                target_list.append(event)
                if len(target_list) >= 1:
                    stream.cancel()
                    break

        task_a = asyncio.create_task(consume(received_a))
        task_b = asyncio.create_task(consume(received_b))

        await _wait_for_subscribers(servicer, count=2)

        await servicer.event_bus.publish("broadcast_event", {"msg": "hello"})

        await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5.0)

        assert len(received_a) == 1
        assert len(received_b) == 1
        assert received_a[0].event_type == "broadcast_event"
        assert received_b[0].event_type == "broadcast_event"

    @pytest.mark.asyncio
    async def test_stream_event_data_fidelity(self, grpc_service):
        """Event type, data map, and timestamp survive gRPC serialization."""
        servicer, stub = grpc_service

        request = game_coordinator_pb2.StreamEventsRequest()
        received = []

        async def consume():
            stream = stub.StreamGameEvents(request)
            async for event in stream:
                received.append(event)
                if len(received) >= 1:
                    stream.cancel()
                    break

        task = asyncio.create_task(consume())
        await _wait_for_subscribers(servicer)

        await servicer.event_bus.publish(
            "player_death",
            {
                "serial": "AA:BB:CC:DD",
                "killer": "EE:FF:00:11",
                "accel_magnitude": "3.14",
            },
        )

        await asyncio.wait_for(task, timeout=5.0)

        event = received[0]
        assert event.event_type == "player_death"
        assert event.data["serial"] == "AA:BB:CC:DD"
        assert event.data["killer"] == "EE:FF:00:11"
        assert event.data["accel_magnitude"] == "3.14"
        assert event.timestamp > 0

    @pytest.mark.asyncio
    async def test_stream_client_disconnect_unsubscribes(self, grpc_service):
        """Cancelling stream triggers EventBus unsubscribe cleanup."""
        servicer, stub = grpc_service

        request = game_coordinator_pb2.StreamEventsRequest()

        stream = stub.StreamGameEvents(request)
        # Wait for subscription
        await _wait_for_subscribers(servicer)
        assert servicer.event_bus.subscriber_count == 1

        # Cancel the stream from client side
        stream.cancel()

        # Give the server time to clean up
        await asyncio.sleep(0.3)

        assert servicer.event_bus.subscriber_count == 0


# ---------------------------------------------------------------------------
# StreamGameEvents — game start via stream
# ---------------------------------------------------------------------------


class TestStreamGameStart:
    """StreamGameEvents with start_config to start a game."""

    @pytest.mark.asyncio
    async def test_stream_start_game_success(self, grpc_service):
        """Send start_config → game starts, stream receives events from event bus."""
        servicer, stub = grpc_service

        config = _make_start_config("FFA", num_players=3)
        request = game_coordinator_pb2.StreamEventsRequest(start_config=config)
        received = []

        # Patch _run_game_loop_threaded so no real background thread starts
        with _NO_THREAD:

            async def consume():
                stream = stub.StreamGameEvents(request)
                async for event in stream:
                    received.append(event)
                    if len(received) >= 3:
                        stream.cancel()
                        break

            task = asyncio.create_task(consume())
            await _wait_for_subscribers(servicer)
            await _wait_for_received(received)  # the starter's own game_start

            # Servicer should have transitioned to STARTING
            assert servicer.game_state == game_coordinator_pb2.GameState.STARTING

            # Publish lifecycle events (normally done by the game in the background thread)
            await servicer.event_bus.publish("game_started", {"game_id": servicer.game_id})
            await servicer.event_bus.publish("game_ended", {"reason": "winner"})

            await asyncio.wait_for(task, timeout=5.0)

        # The starter sees its own game_start first (#1783, UC-003 BR-003).
        assert [e.event_type for e in received] == ["game_start", "game_started", "game_ended"]
        assert received[0].game_id == servicer.game_id

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A3")
    async def test_stream_start_game_too_few_players(self, grpc_service):
        """start_config with 1 player → receive game_start_error event."""
        _servicer, stub = grpc_service

        config = _make_start_config("FFA", num_players=1)
        request = game_coordinator_pb2.StreamEventsRequest(start_config=config)

        events = []
        async for event in stub.StreamGameEvents(request):
            events.append(event)

        assert len(events) == 1
        assert events[0].event_type == "game_start_error"
        assert "at least 2 players" in events[0].data["error"].lower()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A3")
    async def test_stream_start_game_already_running(self, grpc_service):
        """Second start attempt → game_start_error."""
        servicer, stub = grpc_service

        # Occupy the single (default cap=1) game slot with a real primary game.
        with _NO_THREAD:
            _, game_id = await servicer._start_game_from_config(_make_start_config("FFA", num_players=3), _MockSpan())
            await servicer.event_bus.publish(GameEvent.GAME_STARTED, {"game_id": game_id})

            config = _make_start_config("FFA", num_players=3)
            request = game_coordinator_pb2.StreamEventsRequest(start_config=config)

            events = []
            async for event in stub.StreamGameEvents(request):
                events.append(event)

        assert len(events) == 1
        assert events[0].event_type == "game_start_error"
        assert "already in progress" in events[0].data["error"].lower()

    @pytest.mark.asyncio
    async def test_stream_start_game_unknown_mode(self, grpc_service):
        """Invalid game_name → game_error event published by background loop."""
        servicer, stub = grpc_service

        # The servicer validates player count in _start_game_from_config but
        # unknown mode is caught by GameFactory in _run_game_loop_async.
        # With a patched thread, we simulate that error via event_bus.
        config = _make_start_config("TotallyFakeGame", num_players=3)
        request = game_coordinator_pb2.StreamEventsRequest(start_config=config)
        received = []

        with _NO_THREAD:

            async def consume():
                stream = stub.StreamGameEvents(request)
                async for event in stream:
                    received.append(event)
                    if len(received) >= 2:
                        stream.cancel()
                        break

            task = asyncio.create_task(consume())
            await _wait_for_subscribers(servicer)
            await _wait_for_received(received)  # the starter's own game_start

            # Simulate the error that GameFactory would publish
            await servicer.event_bus.publish("game_error", {"error": "Unknown game mode: 'TotallyFakeGame'"})

            await asyncio.wait_for(task, timeout=5.0)

        assert [e.event_type for e in received] == ["game_start", "game_error"]
        assert "Unknown game mode" in received[1].data["error"]

    @pytest.mark.asyncio
    async def test_stream_game_lifecycle(self, grpc_service):
        """Full lifecycle: start → game_started → player_death → game_ended."""
        servicer, stub = grpc_service

        config = _make_start_config("FFA", num_players=4)
        request = game_coordinator_pb2.StreamEventsRequest(start_config=config)
        received = []

        with _NO_THREAD:

            async def consume():
                stream = stub.StreamGameEvents(request)
                async for event in stream:
                    received.append(event)
                    if len(received) >= 5:
                        stream.cancel()
                        break

            task = asyncio.create_task(consume())
            await _wait_for_subscribers(servicer)
            await _wait_for_received(received)  # the starter's own game_start

            # Simulate full game lifecycle
            await servicer.event_bus.publish(
                "game_started",
                {"game_id": servicer.game_id, "game_name": "FFA"},
            )
            await servicer.event_bus.publish(
                "player_death",
                {"serial": "p0", "killer": "p1", "accel_magnitude": "3.5"},
            )
            await servicer.event_bus.publish(
                "player_death",
                {"serial": "p2", "killer": "p3", "accel_magnitude": "2.8"},
            )
            await servicer.event_bus.publish(
                "game_ended",
                {"winner": "p1", "reason": "last_player_standing"},
            )

            await asyncio.wait_for(task, timeout=5.0)

        types = [e.event_type for e in received]
        assert types == ["game_start", "game_started", "player_death", "player_death", "game_ended"]

        # Verify death event data survived round-trip
        death = received[2]
        assert death.data["serial"] == "p0"
        assert death.data["killer"] == "p1"

        # Verify game_ended data
        ended = received[4]
        assert ended.data["winner"] == "p1"


async def _async_noop(*_args, **_kwargs):
    pass


class _FrameServer(controller_manager_pb2_grpc.ControllerManagerServiceServicer):
    """Real gRPC gameplay stream: numbered frames every 10 ms (timestamp = sequence).

    ``first_call_fault`` breaks only the first call after 5 frames: "abort" ends it
    with UNAVAILABLE (a controller-manager restart), "stall" sends nothing more.
    """

    def __init__(self, serials, first_call_fault=None):
        self.serials = serials
        self.first_call_fault = first_call_fault
        self.calls = 0

    async def StreamGameplayData(self, request_iterator, context):  # noqa: N802 - gRPC method name
        self.calls += 1
        faulty = self.calls == 1 and self.first_call_fault is not None

        async def read_client():
            async for _ in request_iterator:
                pass

        reader = asyncio.create_task(read_client())
        try:
            seq = 0
            while True:
                if faulty and seq == 5:
                    if self.first_call_fault == "abort":
                        await context.abort(grpc.StatusCode.UNAVAILABLE, "controller manager restart")
                    await asyncio.Event().wait()
                yield controller_manager_pb2.GameplayDataUpdate(
                    controllers=[
                        controller_manager_pb2.GameplayData(
                            serial=s, accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0)
                        )
                        for s in self.serials
                    ],
                    timestamp=seq,
                )
                seq += 1
                await asyncio.sleep(0.01)
        finally:
            reader.cancel()


class TestEarlyStreamDrain:
    """UC-021 A1 (D-G10): stopping the formation drain keeps the gRPC call alive."""

    @pytest.mark.uc("UC-021", flow="A1")
    @pytest.mark.asyncio
    async def test_stopped_drain_leaves_frames_for_warmup_and_game_loop(self):
        from services.game_coordinator.games.random_teams import RandomTeamsGame

        serials = ["p0", "p1", "p2", "p3"]
        server = grpc.aio.server()
        controller_manager_pb2_grpc.add_ControllerManagerServiceServicer_to_server(_FrameServer(serials), server)
        port = server.add_insecure_port("[::]:0")
        await server.start()
        channel = grpc.aio.insecure_channel(f"localhost:{port}")
        try:
            game = RandomTeamsGame(
                controller_manager_client=controller_manager_pb2_grpc.ControllerManagerServiceStub(channel),
                event_publisher=_async_noop,
                game_id="test_early_drain_grpc",
                num_teams=2,
                initial_players=[game_coordinator_pb2.Player(serial=s) for s in serials],
                rng_seed=7,
            )
            await game._initialize_players()
            await game._start_gameplay_stream()
            game._start_early_stream_drain()
            drain = game._early_drain_task
            await asyncio.sleep(0.3)
            await game._stop_early_stream_drain()

            assert drain.done() and not drain.cancelled()
            assert not game.gameplay_stream.done(), "stopping the drain ended the gameplay call"

            primed = []
            real_update_ema = game._update_ema

            def update_ema(player, accel_mag, weight=4.0):
                primed.append(player.serial)
                real_update_ema(player, accel_mag, weight)

            game._update_ema = update_ema
            await asyncio.wait_for(game._warmup_ema(), timeout=5)
            assert primed, "warmup got no frame after the drain stopped"

            processed = []

            async def process(update):
                processed.append(update.timestamp)
                if len(processed) >= 3:
                    game.running = False
                return False

            game._process_gameplay_update = process
            game.running = True
            await asyncio.wait_for(game._game_loop(), timeout=5)
            assert len(processed) >= 3, "the game loop got no frame after the drain stopped"
            assert processed == sorted(processed)
        finally:
            await channel.close()
            await server.stop(grace=0)

    @pytest.mark.uc("UC-021", flow="A1")
    @pytest.mark.asyncio
    @pytest.mark.parametrize("fault", ["abort", "stall"])
    async def test_stream_broken_during_formation_is_reopened_and_the_game_runs(self, fault):
        """An error, or no frame at the drain stop, finished the call: it is opened once more."""
        from services.game_coordinator.games import random_teams

        serials = ["p0", "p1", "p2", "p3"]
        frame_server = _FrameServer(serials, first_call_fault=fault)
        server = grpc.aio.server()
        controller_manager_pb2_grpc.add_ControllerManagerServiceServicer_to_server(frame_server, server)
        port = server.add_insecure_port("[::]:0")
        await server.start()
        channel = grpc.aio.insecure_channel(f"localhost:{port}")
        events = []

        async def publish(event_type, _data=None, *_args, **_kwargs):
            events.append(str(event_type))

        try:
            with patch.object(random_teams, "TEAM_FORMATION_DURATION", 1):
                game = random_teams.RandomTeamsGame(
                    controller_manager_client=controller_manager_pb2_grpc.ControllerManagerServiceStub(channel),
                    event_publisher=publish,
                    game_id=f"test_early_drain_{fault}",
                    num_teams=2,
                    initial_players=[game_coordinator_pb2.Player(serial=s) for s in serials],
                    rng_seed=7,
                )
                processed = []
                real_process = game._process_gameplay_update

                async def process(update):
                    processed.append(update.timestamp)
                    if len(processed) >= 3:
                        game.force_end("test_done")
                    return await real_process(update)

                game._process_gameplay_update = process
                await asyncio.wait_for(game.run(), timeout=20)

            assert GameEvent.GAME_ERROR.value not in events
            assert GameEvent.COUNTDOWN_START.value in events
            assert len(processed) >= 3, "the game loop got no frame after the stream broke in the formation"
            assert frame_server.calls == 2, "the broken formation stream was not reopened exactly once"
        finally:
            await channel.close()
            await server.stop(grace=0)
