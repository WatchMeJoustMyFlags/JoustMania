"""
GameSession - one running game's state container (#775).

Phase 1 of the shadow-games work (#774). Promotes the coordinator's formerly
singular fields (`game_id`, `game_name`, `players`, config, state,
`current_game`, `event_bus`, thread, running flag, parent trace context) into a
``GameSession`` object so the servicer can hold ``dict[game_id, GameSession]``
and run multiple games concurrently.

Each game already runs in its own background thread with its own asyncio loop
and its own ``GrpcClientManager`` created inside that loop — the per-game
isolation primitive already existed. This class moves that loop logic to operate
on a session instead of on servicer globals. The finicky thread/loop cleanup
ordering from the original servicer is replicated here exactly.

Session kinds:
- ``primary``: the menu-driven game. Publishes to the servicer's persistent
  primary EventBus (so zero-arg ``StreamGameEvents`` subscribers — the menu —
  keep working with no changes). Only the primary session resets the
  single-game state gauges (music_tempo, sensitivity, thresholds) at end.
- ``shadow``: agent/test sessions. Each gets its OWN EventBus and never touches
  the primary game's gauges.
"""

import asyncio
import logging
import threading
import time

from lib.feature_flags import (
    GAME_KIND_REAL as EVAL_GAME_KIND_REAL,
)
from lib.feature_flags import (
    GAME_KIND_SHADOW as EVAL_GAME_KIND_SHADOW,
)
from lib.feature_flags import (
    set_game_session_kind_context,
)
from lib.telemetry import get_tracer
from lib.types import get_game_display_name
from proto import game_coordinator_pb2
from services.game_coordinator import metrics
from services.game_coordinator.event_bus import EventBus
from services.game_coordinator.game_factory import GameFactory
from services.game_coordinator.grpc_clients import GrpcClientManager

logger = logging.getLogger(__name__)

tracer = get_tracer(__name__)

GAME_KIND_PRIMARY = "primary"
GAME_KIND_SHADOW = "shadow"


class GameSession:
    """State container + runtime for a single concurrent game.

    The session owns its EventBus, background thread, asyncio loop, gRPC clients,
    and lifecycle state. ``game_kind`` drives the metric label and whether the
    single-game state gauges are reset on end.
    """

    def __init__(
        self,
        game_id: str,
        game_name: str,
        players: list,
        game_config,
        event_bus: EventBus,
        game_kind: str,
        parent_context,
    ):
        self.game_id = game_id
        self.game_name = game_name
        self.players: list[game_coordinator_pb2.Player] = players
        self.game_config = game_config
        self.event_bus = event_bus
        self.game_kind = game_kind
        self.game_parent_context = parent_context

        self.game_start_time = time.time()
        self.game_state = game_coordinator_pb2.GameState.STARTING
        self.current_game = None

        # Per-session loop runs in its own thread with its own GrpcClientManager.
        self.clients = GrpcClientManager()
        self.game_thread: threading.Thread | None = None
        self.game_running = False

        # Protects game_state, current_game, players for this session.
        self._state_lock = threading.Lock()

    @property
    def is_primary(self) -> bool:
        return self.game_kind == GAME_KIND_PRIMARY

    def on_event_state_sync(self, event_type: str) -> None:
        """EventBus state-sync callback bound to THIS session's state (#775).

        Mutates only this session's ``game_state`` — never servicer globals — so
        a shadow game's lifecycle events don't move the primary game's state.
        Called by EventBus.publish() while holding the event lock.
        """
        from lib.types import GameEvent

        if event_type == GameEvent.GAME_STARTED:
            self.game_state = game_coordinator_pb2.GameState.RUNNING
            logger.info(f"Game {self.game_id} state transitioned to RUNNING")
        elif GameEvent.is_game_ending(event_type):
            self.game_state = game_coordinator_pb2.GameState.ENDED
            logger.info(f"Game {self.game_id} state transitioned to ENDED")

    def start_thread(self) -> None:
        """Launch the background game thread for this session."""
        self.game_running = True
        self.game_thread = threading.Thread(target=self._run_game_loop_threaded, daemon=True)
        self.game_thread.start()

    def _run_game_loop_threaded(self) -> None:
        """Run the game loop in a background thread (creates its own event loop).

        Replicates the original servicer cleanup ordering exactly: cancel pending
        tasks, let gRPC pollers drain, then close the loop (prevents
        BlockingIOError from gRPC's PollerCompletionQueue).
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_game_loop_async())
        finally:
            # Properly cleanup gRPC async resources before closing loop
            # This prevents BlockingIOError from gRPC's PollerCompletionQueue
            try:
                # Cancel any remaining tasks
                pending = asyncio.all_tasks(loop)
                if pending:
                    logger.debug(f"Cancelling {len(pending)} pending tasks before loop close")
                    for task in pending:
                        task.cancel()
                    # Wait for cancellation to complete (with timeout)
                    loop.run_until_complete(asyncio.wait(pending, timeout=1.0))

                # Give gRPC pollers time to drain their queues
                loop.run_until_complete(asyncio.sleep(0.1))
            except Exception as e:
                logger.debug(f"Cleanup before loop close: {e}")
            finally:
                loop.close()

    def _eval_game_kind(self) -> str:
        """Map this session's kind to the eval-context game_kind (#932).

        The session kind is "primary"/"shadow" (metric-label semantics, #775); the
        agent's experiment writer scopes targeting on "game_kind != real"
        (targeting.go). Only a SHADOW session resolves experiments; a primary
        (menu-driven, player-facing) game is the protected "real" baseline.
        """
        return EVAL_GAME_KIND_SHADOW if self.game_kind == GAME_KIND_SHADOW else EVAL_GAME_KIND_REAL

    async def _run_game_loop_async(self) -> None:
        """Run the async game loop for this session."""
        # Initialize async gRPC clients in this event loop
        await self.clients.connect()

        # Establish the shadow/real eval-context split for THIS session's async
        # context BEFORE the game is constructed (#932). GameFactory.create_game()
        # below runs the game mode's __init__, which reads init-frozen calibration
        # flags (thresholds, grace period, per-mode config). Those reads happen in
        # this contextvars context, so the game_kind must already be set here —
        # otherwise a shadow session's init-time reads would fall back to the
        # API-level "real" default and miss its experiments. (BaseGameMode._run
        # later re-sets the full transaction context with game_mode/sensitivity,
        # carrying the same game_kind.) This is contextvars-scoped, so it never
        # leaks across sessions.
        set_game_session_kind_context(self._eval_game_kind())

        # Get the display name for the game span
        game_span_name = get_game_display_name(self.game_name)

        # Parent context captured from the StartGame RPC span keeps the game span
        # in the same trace as the StartGame call.
        parent_context = self.game_parent_context

        with tracer.start_as_current_span(game_span_name, context=parent_context) as game_span:
            game_span.set_attribute("game.name", self.game_name)
            game_span.set_attribute("game.id", self.game_id)
            game_span.set_attribute("game.kind", self.game_kind)
            game_span.set_attribute("player.count", len(self.players))

            try:
                # Check if gRPC clients are available
                if not self.clients.is_connected:
                    error_msg = "gRPC clients not initialized - ControllerManager service must be running"
                    logger.error(error_msg)
                    with self._state_lock:
                        self.game_state = game_coordinator_pb2.GameState.ENDED
                    await self.event_bus.publish("game_error", {"error": error_msg})
                    await self.clients.close()
                    return

                # Create game instance using factory
                try:
                    game = GameFactory.create_game(
                        game_name=self.game_name,
                        controller_manager_client=self.clients.controller_manager,
                        event_publisher=self.event_bus.publish,
                        audio_client=self.clients.audio,
                        game_id=self.game_id,
                        initial_players=self.players,
                        sensitivity=self.game_config.sensitivity if self.game_config else 2,
                        game_config=self.game_config,
                    )
                except ValueError as e:
                    error_msg = str(e)
                    logger.error(error_msg)
                    with self._state_lock:
                        self.game_state = game_coordinator_pb2.GameState.ENDED
                    await self.event_bus.publish("game_error", {"error": error_msg})
                    await self.clients.close()
                    return

                # Tell the game which session it belongs to so its end-of-game
                # analytics cleanup is per-session and only the primary resets
                # the single-game state gauges (#775).
                game.game_kind = self.game_kind
                game._reset_global_gauges_on_end = self.is_primary

                # Store reference and run game
                self.current_game = game
                await game.run()
                logger.info(f"{self.game_name} game completed")

            except Exception as e:
                logger.error(f"Game loop error: {e}", exc_info=True)
                with self._state_lock:
                    self.game_state = game_coordinator_pb2.GameState.ENDED
                await self.event_bus.publish("game_error", {"error": str(e)})
            finally:
                # Thread-safe state cleanup
                with self._state_lock:
                    self.game_running = False
                    self.current_game = None
                    # Defensive: the event-sync callback normally sets ENDED
                    # when the game publishes its ending event, but the
                    # session must reach ENDED even if a mode exits without
                    # one — otherwise it would occupy a concurrency slot
                    # forever (#775 natural-end retirement relies on this).
                    self.game_state = game_coordinator_pb2.GameState.ENDED

                # Update lifecycle metrics for THIS session's kind (#775).
                metrics.active_game.labels(game_kind=self.game_kind, game_id=self.game_id).set(0)
                metrics.active_players.labels(game_kind=self.game_kind, game_id=self.game_id).set(0)
                if self.is_primary:
                    # players_alive is a single global gauge (not yet per-kind);
                    # only the primary session resets it.
                    metrics.players_alive.set(0)
                if self.game_name:
                    metrics.games_completed_total.labels(mode=self.game_name, game_kind=self.game_kind).inc()
                if self.game_start_time:
                    duration = time.time() - self.game_start_time
                    metrics.game_duration_seconds.labels(game_kind=self.game_kind, game_id=self.game_id).set(duration)

                # Cleanup channels
                await self.clients.close()
                logger.info(f"Closed gRPC channels for game {self.game_id}")

    async def force_end(self, reason: str) -> tuple[bool, str]:
        """Force end this session's game.

        Stops the loop, calls ``force_end()`` on the live game, joins the thread,
        transitions to ENDED, and publishes ``game_force_ended``. No-ops safely
        when this session is not in a startable/running state.
        """
        with self._state_lock:
            if self.game_state not in [
                game_coordinator_pb2.GameState.STARTING,
                game_coordinator_pb2.GameState.RUNNING,
            ]:
                return False, "No game in progress"

            self.game_running = False
            current_game = self.current_game
            game_thread = self.game_thread

        if current_game and hasattr(current_game, "force_end"):
            current_game.force_end()

        # Join in executor to avoid blocking the gRPC server.
        if game_thread and game_thread.is_alive():
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: game_thread.join(timeout=2.0))

        with self._state_lock:
            self.game_state = game_coordinator_pb2.GameState.ENDED

        await self.event_bus.publish(
            "game_force_ended",
            {"reason": reason, "game_id": self.game_id},
        )

        logger.info(f"Force ended game {self.game_id}: {reason}")
        return True, ""

    async def join_thread(self, join_timeout_s: float = 2.0) -> None:
        """Join this session's game thread (used on servicer shutdown)."""
        with self._state_lock:
            self.game_running = False
            game_thread = self.game_thread

        if game_thread and game_thread.is_alive():
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: game_thread.join(timeout=join_timeout_s))

        await self.clients.close()
