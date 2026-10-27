"""
Cross-loop EventBus regression tests (#1300, UC-027 BR-002).

Each game runs on its own ``asyncio.new_event_loop()`` in a thread
(game_session.py), while the servicer's main loop subscribes, publishes
GAME_START / ``game_force_ended`` and intervention events on the SAME persistent
primary bus. The bus used to hold an ``asyncio.Lock``: it bound to the first
loop that had to wait for it, so once a game loop that had bound it closed, the
next contended publish from any other loop raised "bound to a different event
loop" and new games failed at GAME_STARTING until the coordinator restarted.

These tests drive the bus from real threads with their own loops, the way
GameSession does.
"""

import asyncio
import concurrent.futures
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from opentelemetry.trace import INVALID_SPAN_CONTEXT

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from lib.types import GameEvent
from proto import game_coordinator_pb2
from services.game_coordinator import game_session as game_session_mod
from services.game_coordinator.event_bus import EventBus
from services.game_coordinator.servicer import GameCoordinatorServicer

HOLD_EVENT = "test_hold_bus_lock"


def _run_on_own_loop(coro_fn) -> tuple[threading.Thread, dict]:
    """Run ``coro_fn()`` on a fresh event loop in a thread, then close the loop.

    Mirrors GameSession._run_game_loop_threaded. A heartbeat task keeps the loop
    waking up like a real 60 Hz game loop does. Returns the started thread and an
    outcome dict whose ``error`` holds what the coroutine raised (None if nothing).
    """
    outcome: dict[str, BaseException | None] = {"error": None}

    def _thread_main():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _main():
            async def _heartbeat():
                while True:  # noqa: ASYNC110 - periodic wake-ups, not a condition wait
                    await asyncio.sleep(0.01)

            heartbeat = asyncio.create_task(_heartbeat())
            try:
                await asyncio.wait_for(coro_fn(), timeout=3.0)
            finally:
                heartbeat.cancel()

        try:
            loop.run_until_complete(_main())
        except BaseException as e:  # noqa: BLE001 - surfaced to the test
            outcome["error"] = e
        finally:
            loop.close()

    thread = threading.Thread(target=_thread_main, daemon=True)
    thread.start()
    return thread, outcome


class _LockHolder:
    """Makes the main loop hold the bus lock while a game thread publishes.

    The state-sync callback runs under the bus lock, so blocking in it on the
    ``HOLD_EVENT`` publish keeps the lock taken. This forces the contention that
    happens in production when the main loop (subscribe, force-end, intervention
    events) and a game loop use the primary bus at the same moment.
    """

    def __init__(self):
        self.holding = threading.Event()
        self.release = threading.Event()

    def callback(self, event_type: str) -> None:
        if event_type == HOLD_EVENT:
            self.holding.set()
            self.release.wait(timeout=2.0)

    async def publish_from_game_thread_while_held(self, bus: EventBus, event_type: str, game_id: str):
        """Publish ``event_type`` from a new thread loop while the main loop holds the lock."""
        self.holding.clear()
        self.release.clear()

        async def _game_publish():
            self.holding.wait(timeout=2.0)
            # Release the main loop's hold shortly after this publish starts waiting.
            threading.Timer(0.2, self.release.set).start()
            await bus.publish(event_type, {"game_id": game_id})

        thread, outcome = _run_on_own_loop(_game_publish)
        # Blocks the main thread inside the callback (lock held) until released.
        await bus.publish(HOLD_EVENT, {})
        await asyncio.get_running_loop().run_in_executor(None, lambda: thread.join(timeout=5.0))
        assert not thread.is_alive(), "game thread did not finish"
        return outcome["error"]


async def _next_event(queue: asyncio.Queue, event_type: str, deadline_s: float = 1.0):
    """Return the next event of ``event_type`` from ``queue`` (skipping others)."""
    async with asyncio.timeout(deadline_s):
        while True:
            event = await queue.get()
            if event.event_type == event_type:
                return event


@pytest.mark.uc("UC-027", br=["BR-002"])
@pytest.mark.asyncio
async def test_new_game_on_new_loop_publishes_after_force_ended_game():
    """Reproduces #1300: game 1 on loop A binds the bus, is force-ended (loop A
    closes), then game 2 on loop B publishes GAME_STARTING while the main loop
    uses the bus. On the old asyncio.Lock this raised "bound to a different
    event loop"; it must now succeed and reach the main-loop subscriber.
    """
    holder = _LockHolder()
    bus = EventBus(state_sync_callback=holder.callback)
    menu_queue = await bus.subscribe("menu")

    # Game 1 on loop A, contending with the main loop.
    error = await holder.publish_from_game_thread_while_held(bus, GameEvent.GAME_STARTING, "game_1")
    assert error is None, f"game 1 publish failed: {error!r}"

    # Force end (main loop); game 1's loop is already closed.
    await bus.publish("game_force_ended", {"reason": "dry_run", "game_id": "game_1"})

    # Game 2 on a NEW loop B, contending with the main loop again.
    error = await holder.publish_from_game_thread_while_held(bus, GameEvent.GAME_STARTING, "game_2")
    assert error is None, f"game 2 publish failed after force-end: {error!r}"

    received = []
    while not menu_queue.empty():
        received.append(menu_queue.get_nowait().event_type)
    # The cross-thread deliveries were scheduled with call_soon_threadsafe; let them run.
    await asyncio.sleep(0.05)
    while not menu_queue.empty():
        received.append(menu_queue.get_nowait().event_type)
    assert received.count(GameEvent.GAME_STARTING) == 2
    assert "game_force_ended" in received


@pytest.mark.uc("UC-027", br=["BR-002"])
def test_publish_from_game_loop_wakes_main_loop_subscriber_promptly():
    """A publish from a game thread's loop must wake a subscriber waiting on the
    main loop right away. ``asyncio.Queue.put_nowait`` from another thread wakes
    the reader from the wrong thread (under debug it raises; otherwise the reader
    sleeps until its next timeout), so delivery must hop loops safely.

    The "main" loop runs in its own thread here so a reader stuck by the old
    behaviour fails the test instead of hanging the pytest loop.
    """
    main_loop = asyncio.new_event_loop()
    main_loop.set_debug(True)
    main_thread = threading.Thread(target=main_loop.run_forever, daemon=True)
    main_thread.start()
    try:
        bus = EventBus()
        queue = asyncio.run_coroutine_threadsafe(bus.subscribe("menu"), main_loop).result(timeout=2.0)
        reader = asyncio.run_coroutine_threadsafe(queue.get(), main_loop)
        time.sleep(0.05)  # let the reader block on queue.get()

        async def _game_publish():
            await bus.publish(GameEvent.GAME_STARTED, {"game_id": "game_2"})

        game_thread, outcome = _run_on_own_loop(_game_publish)
        game_thread.join(timeout=5.0)
        assert outcome["error"] is None

        try:
            event = reader.result(timeout=0.5)
        except concurrent.futures.TimeoutError:
            pytest.fail("main-loop subscriber was not woken by the game-loop publish")
        assert event.event_type == GameEvent.GAME_STARTED
        assert event.data["game_id"] == "game_2"
    finally:
        main_loop.call_soon_threadsafe(main_loop.stop)
        main_thread.join(timeout=2.0)


@pytest.mark.uc("UC-027", br=["BR-002"])
@pytest.mark.asyncio
async def test_subscriber_on_closed_loop_is_dropped_not_fatal():
    """A subscriber whose loop has closed cannot read its queue again: publishing
    must drop it and keep delivering to everyone else, never raise."""
    bus = EventBus()
    live_queue = await bus.subscribe("live")

    async def _subscribe_and_leave():
        await bus.subscribe("gone")

    thread, outcome = _run_on_own_loop(_subscribe_and_leave)
    await asyncio.get_running_loop().run_in_executor(None, lambda: thread.join(timeout=5.0))
    assert outcome["error"] is None
    assert bus.subscriber_count == 2

    with patch("services.game_coordinator.metrics.event_bus_publish_errors_total") as errors_total:
        await bus.publish("event1", {})

    assert list(bus._subscribers) == ["live"]
    assert live_queue.get_nowait().event_type == "event1"
    errors_total.labels.assert_called_once_with(event_type="event1")


class _MockSpan:
    def get_span_context(self):
        return None


def _menu_config(serials):
    return game_coordinator_pb2.StartGameConfig(
        game_name="FFA",
        players=[game_coordinator_pb2.Player(serial=s) for s in serials],
        sensitivity=2,
        origin=game_coordinator_pb2.GAME_ORIGIN_MENU,
    )


async def _fake_game_loop(self):
    """Stand-in for GameSession._run_game_loop_async: no gRPC, same bus traffic.

    Runs on the session's own thread loop, like a real game.
    """
    await self.event_bus.publish(GameEvent.GAME_STARTING, {"game_id": self.game_id})
    await self.event_bus.publish(GameEvent.GAME_STARTED, {"game_id": self.game_id})
    while self.game_running:  # noqa: ASYNC110 - polls the flag force_end clears, like BaseGameMode
        await asyncio.sleep(0.01)


async def _wait_for_state(session, state, deadline_s=2.0):
    # game_state is set from the game thread; there is no event to wait on.
    async with asyncio.timeout(deadline_s):
        while session.game_state != state:  # noqa: ASYNC110
            await asyncio.sleep(0.01)


@pytest.mark.uc("UC-027", br=["BR-002"])
@pytest.mark.asyncio
async def test_servicer_starts_new_primary_game_after_force_end_on_real_threads():
    """End to end through the servicer with real per-game thread loops: game 1
    runs, is force-ended, then game 2 starts, reaches RUNNING, and the menu's
    zero-arg subscriber on the main loop receives game 2's events."""
    servicer = GameCoordinatorServicer()
    menu_queue = await servicer.primary_event_bus.subscribe("menu")
    with patch.object(game_session_mod.GameSession, "_run_game_loop_async", _fake_game_loop):
        ok, game_1 = await servicer._start_game_from_config(_menu_config(("a1", "a2")), _MockSpan())
        assert ok, game_1
        session_1 = servicer.sessions[game_1]
        await _wait_for_state(session_1, game_coordinator_pb2.GameState.RUNNING)

        resp = await servicer.ForceEndGame(
            game_coordinator_pb2.ForceEndGameRequest(reason="dry_run"),
            None,
        )
        assert resp.success, resp.error
        assert not session_1.game_thread.is_alive()

        ok, game_2 = await servicer._start_game_from_config(_menu_config(("b1", "b2")), _MockSpan())
        assert ok, game_2
        session_2 = servicer.sessions[game_2]
        assert session_2.event_bus is servicer.primary_event_bus
        await _wait_for_state(session_2, game_coordinator_pb2.GameState.RUNNING)

        started = await _next_event(menu_queue, GameEvent.GAME_STARTED)
        while started.game_id != game_2:
            started = await _next_event(menu_queue, GameEvent.GAME_STARTED)
        assert started.data["game_id"] == game_2

        resp = await servicer.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason="cleanup"), None)
        assert resp.success, resp.error


async def _publish_from_game_thread(bus: EventBus, events: list[str]) -> None:
    """Publish ``events`` in order from a game thread's own loop, then let the main loop deliver them."""

    async def _game_publish():
        for event_type in events:
            await bus.publish(event_type, {"serial": "p1"})

    thread, outcome = _run_on_own_loop(_game_publish)
    await asyncio.get_running_loop().run_in_executor(None, lambda: thread.join(timeout=5.0))
    assert outcome["error"] is None
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.uc("UC-035", br=["BR-008"])
@pytest.mark.asyncio
async def test_a_half_full_queue_drops_the_energy_event_on_its_own_loop_but_never_a_death():
    """UC-035 BR-008: `player_shield_energy` is dropped in `_deliver` once the queue is half full; deaths are not."""
    bus = EventBus()
    queue = await bus.subscribe("dashboard", max_queue_size=10)
    with patch("services.game_coordinator.event_bus.metrics.event_bus_publish_drops_total") as drops:
        await _publish_from_game_thread(bus, [GameEvent.PLAYER_SHIELD_ENERGY] * 3)
        assert queue.qsize() == 3  # below half: delivered

        await _publish_from_game_thread(
            bus,
            [GameEvent.PLAYER_DEATH, GameEvent.PLAYER_DEATH, GameEvent.PLAYER_SHIELD_ENERGY, GameEvent.PLAYER_DEATH],
        )

    delivered = [queue.get_nowait().event_type for _ in range(queue.qsize())]
    assert delivered == ["player_shield_energy"] * 3 + ["player_death"] * 3
    drops.labels.assert_called_once_with(event_type="player_shield_energy")


@pytest.mark.uc("UC-035", br=["BR-008"])
@pytest.mark.asyncio
async def test_the_energy_event_records_no_span_event():
    """UC-035 BR-008: an energy event adds no span event (the span event limit stays for real events)."""
    bus = EventBus()
    span = _RecordingSpan()
    with patch("services.game_coordinator.event_bus.trace.get_current_span", return_value=span):
        await bus.publish(GameEvent.PLAYER_SHIELD_ENERGY, {"serial": "p1"})
        await bus.publish(GameEvent.PLAYER_DEATH, {"serial": "p1"})
    assert span.events == ["player_death"]


class _RecordingSpan:
    def __init__(self):
        self.events: list[str] = []

    def is_recording(self) -> bool:
        return True

    def add_event(self, name, attributes=None):  # noqa: ARG002 - span API
        self.events.append(str(name))

    def get_span_context(self):
        return INVALID_SPAN_CONTEXT
