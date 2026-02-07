"""
Event Bus - Pub/sub system for game coordinator events.

Provides thread-safe event publishing and async streaming for game events:
- Multiple subscribers via async queues
- State synchronization callbacks
- OpenTelemetry span event recording

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

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from proto import game_coordinator_pb2
from services.game_coordinator import metrics

logger = logging.getLogger(__name__)


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
                                 Called while holding the state lock.
        """
        self._subscribers: dict[str, asyncio.Queue] = {}
        # Single asyncio.Lock for all _subscribers access (subscribe, unsubscribe, publish)
        self._event_lock = asyncio.Lock()
        self._state_sync_callback = state_sync_callback

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
        async with self._event_lock:
            self._subscribers[subscriber_id] = event_queue
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
        async with self._event_lock:
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
        # Subscriber snapshot and state sync under single async lock
        async with self._event_lock:
            # Call state sync callback if provided
            if self._state_sync_callback:
                self._state_sync_callback(event_type)

            # Snapshot subscribers to avoid dict modification during iteration
            subscribers_snapshot = dict(self._subscribers)

        # Record as span event for observability
        current_span = trace.get_current_span()
        if current_span.is_recording():
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

        # Create protobuf event
        event = game_coordinator_pb2.GameEvent(
            event_type=event_type,
            data=string_data,
            timestamp=int(time.time() * 1000),
        )

        # Publish to all subscribers (put_nowait is thread-safe for asyncio.Queue)
        for sub_id, event_queue in subscribers_snapshot.items():
            try:
                event_queue.put_nowait(event)
                logger.debug(f"Published {event_type} to subscriber {sub_id}")
            except asyncio.QueueFull:
                metrics.event_bus_publish_drops_total.labels(event_type=event_type).inc()
                logger.warning(f"Subscriber {sub_id} queue full, skipping event")
            except Exception as e:
                metrics.event_bus_publish_errors_total.labels(event_type=event_type).inc()
                logger.error(f"Error publishing to subscriber {sub_id}: {e}")

    async def get_subscriber_ids(self) -> list[str]:
        """
        Get list of current subscriber IDs.

        Returns:
            List of subscriber IDs
        """
        async with self._event_lock:
            return list(self._subscribers.keys())
