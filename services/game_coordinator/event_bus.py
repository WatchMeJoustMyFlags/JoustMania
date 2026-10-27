"""
Event Bus - Pub/sub system for game coordinator events.

Provides thread-safe event publishing and async streaming for game events:
- Multiple subscribers via async queues
- State synchronization callbacks
- OpenTelemetry span event recording

Threading model (#1300): one bus is used from SEVERAL event loops. The
persistent primary bus outlives every game; each game runs on its own
``asyncio.new_event_loop()`` in a thread (game_session.py) and publishes from
there, while the servicer's main loop subscribes (StreamGameEvents), publishes
GAME_START / ``game_force_ended`` and intervention events. So the bus holds no
loop-bound primitive of its own:

- ``_subscribers`` is guarded by a ``threading.Lock``. Nothing awaits while it
  is held, so it never blocks a loop for longer than a dict copy. (An
  ``asyncio.Lock`` binds to the first loop that has to wait for it; once that
  loop was a game loop that then closed, every later contended publish raised
  "bound to a different event loop" until the coordinator restarted.)
- Each subscriber queue belongs to the loop that subscribed. A publish from
  that loop delivers directly; a publish from any other loop hands the event to
  the subscriber's loop with ``call_soon_threadsafe`` (``asyncio.Queue`` is not
  thread-safe: a cross-thread ``put_nowait`` wakes the reader's future from the
  wrong thread).

Usage:
    from services.game_coordinator.event_bus import EventBus

    # Create with optional state sync callback
    event_bus = EventBus(state_sync_callback=handle_state_change)

    # Subscribe (returns queue for receiving events)
    queue = await event_bus.subscribe("subscriber_1")

    # Publish events
    await event_bus.publish("game_started", {"game_id": "123"})

    # Unsubscribe
    await event_bus.unsubscribe("subscriber_1")
"""

import asyncio
import logging
import threading
import time
from collections.abc import Callable

from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from proto import game_coordinator_pb2
from services.game_coordinator import metrics

logger = logging.getLogger(__name__)

# Events a slow subscriber may lose (UC-035 BR-008, telemetry.md §5): the shield
# hold's energy state for the dashboard bar. A droppable event is dropped in
# _deliver, on the subscriber's own loop, once that queue is half full, so it can
# never push out player_death, game_ended or game_winner; it records no span event.
DROPPABLE_EVENT_TYPES = frozenset({"player_shield_energy"})


# UC-027 BR-002: one bus is used from several event loops, so the next game after a force end streams normally.
class EventBus:
    """
    Thread-safe event bus for game coordinator events.

    Supports:
    - Multiple async subscribers via queues
    - State synchronization callbacks for game lifecycle events
    - Span event recording for observability
    """

    def __init__(self, state_sync_callback: Callable[[str], None] | None = None):
        """
        Initialize event bus.

        Args:
            state_sync_callback: Optional callback(event_type) called on each publish.
                                 Used to sync game state (e.g., game_started -> RUNNING).
                                 Called while holding the bus lock, on the
                                 publisher's thread; it must not block.
        """
        # subscriber_id -> (queue, the loop that owns the queue).
        self._subscribers: dict[str, tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = {}
        # Guards _subscribers and the state-sync call. A threading.Lock, not an
        # asyncio.Lock: the bus is used from several event loops (#1300). Never
        # await while holding it.
        self._event_lock = threading.Lock()
        self._state_sync_callback = state_sync_callback
        # game_id stamped onto every published GameEvent (#776). MUTABLE because
        # the persistent primary bus outlives sessions: the primary session sets
        # this when it binds and the servicer clears it on retire, so a game_id
        # fixed at construction would be wrong for the long-lived primary bus.
        # Empty string => no game bound (events published while idle).
        self.current_game_id: str = ""

    @property
    def subscriber_count(self) -> int:
        """Get current number of subscribers."""
        return len(self._subscribers)

    async def subscribe(self, subscriber_id: str, max_queue_size: int = 100) -> asyncio.Queue:
        """
        Subscribe to events.

        Args:
            subscriber_id: Unique subscriber identifier
            max_queue_size: Maximum queue size before events are dropped

        Returns:
            Queue that will receive GameEvent messages
        """
        event_queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        owner_loop = asyncio.get_running_loop()
        with self._event_lock:
            self._subscribers[subscriber_id] = (event_queue, owner_loop)
            metrics.event_bus_active_subscribers.set(len(self._subscribers))
        logger.info(f"New event subscriber: {subscriber_id}")
        return event_queue

    async def unsubscribe(self, subscriber_id: str) -> bool:
        """
        Remove a subscriber.

        Args:
            subscriber_id: Subscriber to remove

        Returns:
            True if subscriber was found and removed, False otherwise
        """
        with self._event_lock:
            if subscriber_id in self._subscribers:
                del self._subscribers[subscriber_id]
                metrics.event_bus_active_subscribers.set(len(self._subscribers))
                logger.info(f"Event subscriber removed: {subscriber_id}")
                return True
        return False

    async def publish(self, event_type: str, data: dict[str, str]):
        """
        Publish an event to all subscribers.

        Args:
            event_type: Type of event (e.g., "game_started", "player_death")
            data: Event data as string key-value pairs
        """
        # Subscriber snapshot and state sync under the bus lock (no awaits inside)
        with self._event_lock:
            # Call state sync callback if provided
            if self._state_sync_callback:
                self._state_sync_callback(event_type)

            # Snapshot subscribers to avoid dict modification during iteration
            subscribers_snapshot = dict(self._subscribers)

        # Record as span event for observability (not a droppable event: UC-035 BR-008)
        current_span = trace.get_current_span()
        if str(event_type) not in DROPPABLE_EVENT_TYPES and current_span.is_recording():
            attributes = {
                "event.type": event_type,
                "subscribers.count": len(subscribers_snapshot),
                **{k: str(v) for k, v in data.items()},
            }
            current_span.add_event(event_type, attributes=attributes)

        # Convert all values to strings (protobuf map<string, string> requirement)
        string_data = {k: str(v) for k, v in data.items()}

        # Inject W3C Trace Context for cross-service propagation
        propagator = TraceContextTextMapPropagator()
        carrier: dict[str, str] = {}
        propagator.inject(carrier)
        if "traceparent" in carrier:
            string_data["_traceparent"] = carrier["traceparent"]
        if "tracestate" in carrier:
            string_data["_tracestate"] = carrier["tracestate"]

        # Create protobuf event, stamped with the game_id this bus is bound to
        # (#776). Empty when no game is bound (e.g. primary bus while idle).
        event = game_coordinator_pb2.GameEvent(
            event_type=event_type,
            data=string_data,
            timestamp=int(time.time() * 1000),
            game_id=self.current_game_id,
        )

        # Deliver on each subscriber's own loop: directly when it is this loop,
        # otherwise via call_soon_threadsafe (asyncio.Queue is not thread-safe).
        current_loop = asyncio.get_running_loop()
        for sub_id, (event_queue, owner_loop) in subscribers_snapshot.items():
            if owner_loop is current_loop:
                self._deliver(sub_id, event_queue, event)
                continue
            try:
                owner_loop.call_soon_threadsafe(self._deliver, sub_id, event_queue, event)
            except RuntimeError as e:
                # The subscriber's loop is closed: nobody can read that queue
                # again, so drop the subscriber instead of failing every publish.
                metrics.event_bus_publish_errors_total.labels(event_type=event_type).inc()
                logger.warning(f"Subscriber {sub_id} loop is closed ({e}); removing it")
                self._discard_subscriber(sub_id, event_queue)

    def _deliver(self, sub_id: str, event_queue: asyncio.Queue, event) -> None:
        """Put one event on a subscriber queue. Runs on the queue's own loop."""
        event_type = event.event_type
        if (
            event_type in DROPPABLE_EVENT_TYPES
            and event_queue.maxsize > 0
            and event_queue.qsize() * 2 >= event_queue.maxsize
        ):
            metrics.event_bus_publish_drops_total.labels(event_type=event_type).inc()
            logger.debug(f"Subscriber {sub_id} queue half full, dropping {event_type}")
            return
        try:
            event_queue.put_nowait(event)
            logger.debug(f"Published {event_type} to subscriber {sub_id}")
        except asyncio.QueueFull:
            metrics.event_bus_publish_drops_total.labels(event_type=event_type).inc()
            logger.warning(f"Subscriber {sub_id} queue full, skipping event")
        except Exception as e:
            metrics.event_bus_publish_errors_total.labels(event_type=event_type).inc()
            logger.error(f"Error publishing to subscriber {sub_id}: {e}")

    def _discard_subscriber(self, sub_id: str, event_queue: asyncio.Queue) -> None:
        """Remove a subscriber if it still owns ``event_queue`` (it may have re-subscribed)."""
        with self._event_lock:
            entry = self._subscribers.get(sub_id)
            if entry is not None and entry[0] is event_queue:
                del self._subscribers[sub_id]
                metrics.event_bus_active_subscribers.set(len(self._subscribers))
