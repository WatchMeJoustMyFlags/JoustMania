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
from contextlib import suppress

from opentelemetry import trace
from opentelemetry.context import Context

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
        stream_span_context=None,
        experiment_parent_span_context=None,
        experiment_id: str = "",
        arm: str = "",
        rng_seed: int = 0,
    ):
        self.game_id = game_id
        self.game_name = game_name
        self.players: list[game_coordinator_pb2.Player] = players
        self.game_config = game_config
        self.event_bus = event_bus
        self.game_kind = game_kind
        # SpanContext of the inbound StreamGameEvents/StartGame RPC span (#1157).
        # The game-lifecycle span is rooted as its OWN trace (a NEW root, not a
        # child of the long-lived stream), and this is added back as an OTel Link
        # so the inbound call stays navigable in Jaeger. None when there is no
        # recordable inbound span (telemetry off / unsampled) — handled gracefully.
        self.stream_span_context = stream_span_context
        # SpanContext of the SPAWNING agent EXPERIMENT span, propagated as the
        # incoming traceparent on the shadow-game spawn RPC (#1182, epic #1181).
        # For a SHADOW + experiment-bound game this becomes the game-lifecycle
        # span's PARENT (causal parent-child, not a Link) so the game span is a
        # CHILD of its long-lived experiment span in the same trace. None for a
        # real game, an unbound shadow game, or when no traceparent was injected
        # — those keep the own-root model (#1157/#1164), handled gracefully.
        self.experiment_parent_span_context = experiment_parent_span_context
        # Experiment attribution within a shadow session (#975, epic #982). Empty
        # for a non-experiment game; set by #976's spawn binding. These are finer-
        # grained labels WITHIN game_kind=shadow, never a replacement for it.
        self.experiment_id = experiment_id
        self.arm = arm
        # Paired CRN seed (#1003): the deterministic per-instance RNG seed for a
        # shadow game in an experiment pair. 0 means entropy (today's behavior).
        # The servicer already zeroed this for a real (primary) session, so a
        # primary game is structurally unable to be seeded — same real-protection
        # guard as experiment_id/arm.
        self.rng_seed = rng_seed

        self.game_start_time = time.time()
        self.game_state = game_coordinator_pb2.GameState.STARTING
        self.current_game = None
        # Hex trace_id of this session's root game span (#1133). Captured once the
        # span exists in _run_game and exposed as the game_trace_correlation gauge's
        # game_trace_id label so the agent can link agent.decision -> this game trace.
        # Empty until the span opens; used at retire to remove the exact gauge series.
        self.game_trace_id = ""
        # Hex span_id of this session's root game span (#1157). Carried alongside
        # game_trace_id on the correlation gauge so the agent's Link references the
        # actual game-start span (Jaeger highlights it via uiFind) rather than an
        # all-zero span id under the trace. Captured/cleared in lockstep with
        # game_trace_id.
        self.game_trace_span_id = ""
        # Hex span_id of this session's gameplay_phase span (#1195). gameplay_phase
        # is the stable active-play sub-span within the game trace; the agent
        # re-parents its in-game decision chain under THIS span instead of the game
        # root (#1187). Empty until the gameplay_phase span opens (base.py calls
        # publish_gameplay_phase_span_id); carried as the gameplay_phase_span_id
        # label on the correlation gauge alongside game_trace_span_id (same trace).
        # Captured/cleared in lockstep with game_trace_id so retire removes the
        # exact gauge series.
        self.gameplay_phase_span_id = ""

        # Per-session loop runs in its own thread with its own GrpcClientManager.
        self.clients = GrpcClientManager()
        self.game_thread: threading.Thread | None = None
        self.game_running = False

        # Protects game_state, current_game, players for this session.
        self._state_lock = threading.Lock()

    @property
    def is_primary(self) -> bool:
        return self.game_kind == GAME_KIND_PRIMARY

    def clear_metrics(self) -> None:
        """Remove this session's game_id-labeled gauge series on retire (#1018).

        ``active_game`` / ``active_players`` / ``game_duration_seconds`` are set
        on start and zeroed on end, but the zeroed series persists in the TSDB
        forever. With concurrent shadow games churning many short-lived game_ids
        this grows the series set unboundedly. Removing the EXACT label tuple
        each was set with (game_kind, game_id, experiment_id, arm) at retire
        keeps cardinality bounded.

        Idempotent: a double-retire or never-started session may have no series
        for some/all of these; the prometheus client raises KeyError/ValueError
        on ``.remove()`` of a missing label set, so each is suppressed. Mirrors
        the per-player cleanup in ``metrics.clear_player_analytics``.
        """
        labels = (self.game_kind, self.game_id, self.experiment_id, self.arm)
        for gauge in (
            metrics.active_game,
            metrics.active_players,
            metrics.game_duration_seconds,
        ):
            with suppress(KeyError, ValueError):
                gauge.remove(*labels)

        # The trace-correlation gauge (#1133/#1157/#1195) carries a different label
        # tuple (game_kind, game_id, game_trace_id, game_trace_span_id,
        # gameplay_phase_span_id) and was only set when the span was sampled
        # (game_trace_id non-empty), so remove it separately and only then. Remove
        # with the CURRENT gameplay_phase_span_id: it is "" until the gameplay_phase
        # span opens and is then updated in lockstep with the gauge re-emit, so this
        # always matches the live series tuple.
        if self.game_trace_id:
            with suppress(KeyError, ValueError):
                metrics.game_trace_correlation.remove(
                    self.game_kind,
                    self.game_id,
                    self.game_trace_id,
                    self.game_trace_span_id,
                    self.gameplay_phase_span_id,
                )

    def publish_gameplay_phase_span_id(self, gameplay_phase_span_id: str) -> None:
        """Re-emit the correlation gauge carrying the gameplay_phase span_id (#1195).

        Called by the game (base.py) when its gameplay_phase span opens, passing
        that span's hex span_id. gameplay_phase is the stable active-play sub-span;
        the agent re-parents its in-game decision chain under it (a strict
        refinement of the #1187 game-root parenting).

        The gauge was already SET to 1 at game-root-span time with an EMPTY
        gameplay_phase_span_id label (different label tuple -> different series).
        To avoid leaving that empty-labeled series behind, REMOVE it first, then SET
        the series carrying the populated id, and update self.gameplay_phase_span_id
        so clear_metrics removes the live tuple at retire.

        No-op when the game span was unsampled (game_trace_id empty -> the gauge was
        never set) or the passed id is empty/invalid: the agent then keeps falling
        back to the game root span (game_trace_span_id), so correlation is never lost.
        """
        if not self.game_trace_id or not gameplay_phase_span_id:
            return
        if gameplay_phase_span_id == self.gameplay_phase_span_id:
            return
        # Remove the previously-set series (empty gameplay id at first publish).
        with suppress(KeyError, ValueError):
            metrics.game_trace_correlation.remove(
                self.game_kind,
                self.game_id,
                self.game_trace_id,
                self.game_trace_span_id,
                self.gameplay_phase_span_id,
            )
        self.gameplay_phase_span_id = gameplay_phase_span_id
        metrics.game_trace_correlation.labels(
            game_kind=self.game_kind,
            game_id=self.game_id,
            game_trace_id=self.game_trace_id,
            game_trace_span_id=self.game_trace_span_id,
            gameplay_phase_span_id=self.gameplay_phase_span_id,
        ).set(1)

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
        # Carry the experiment identity (#975) on the same session boundary as
        # game_kind so an experiment-scoped flag override is in place before the
        # game mode's init-frozen reads. Pass them only when set (absent ⇒ not in
        # any experiment), preserving real-by-default.
        set_game_session_kind_context(
            self._eval_game_kind(),
            experiment_id=self.experiment_id or None,
            arm=self.arm or None,
        )

        # Get the display name for the game span
        game_span_name = get_game_display_name(self.game_name)

        # Root the game-lifecycle span as its OWN trace (#1157), NOT as a child of
        # the long-lived StreamGameEvents stream span. Following the agent's
        # trace-correlation Link then lands on the game itself, and a single bidi
        # stream no longer parents (and bloats) every game's trace. We pass an
        # explicit empty Context so any ambient span in this thread/context is also
        # ignored as a parent. The inbound stream/start-RPC span stays navigable
        # via an OTel Link back to it (omitted when there is no recordable inbound
        # span — telemetry off / unsampled — so we never link to an invalid context).
        links = []
        if self.stream_span_context is not None and self.stream_span_context.trace_id != 0:
            links.append(trace.Link(self.stream_span_context))

        # Choose the game-span PARENT context (#1182, epic #1181).
        #
        # A real (primary) game — and any shadow game NOT bound to an experiment —
        # is rooted as its OWN trace (#1157/#1164): we pass an explicit empty
        # Context so neither the inbound stream span nor any ambient span parents
        # it. The inbound call stays navigable via the Link above.
        #
        # A SHADOW + experiment-bound game whose spawn carried the agent's
        # experiment traceparent is instead made a CHILD of that experiment span
        # (causal parent-child), so it nests under the long-lived experiment span
        # in the same trace. The gate is STRICT (shadow AND experiment_id AND a
        # valid injected parent) so the real-game own-root structure is never
        # touched. We still keep the Link (harmless for shadow, navigability for
        # real).
        parent_context = Context()
        if (
            self.game_kind == GAME_KIND_SHADOW
            and self.experiment_id
            and self.experiment_parent_span_context is not None
            and self.experiment_parent_span_context.trace_id != 0
        ):
            parent_context = trace.set_span_in_context(trace.NonRecordingSpan(self.experiment_parent_span_context))

        with tracer.start_as_current_span(
            game_span_name,
            context=parent_context,
            links=links,
        ) as game_span:
            game_span.set_attribute("game.name", self.game_name)
            game_span.set_attribute("game.id", self.game_id)
            game_span.set_attribute("game.kind", self.game_kind)
            # Experiment attribution (#975). Spans are the PRIMARY attribution
            # channel — unbounded-cardinality experiment ids are fine here (each
            # is one trace). Emitted always (empty when not in an experiment),
            # mirroring game.kind; the agent reads them off the span attributes.
            game_span.set_attribute("experiment.id", self.experiment_id)
            game_span.set_attribute("experiment.arm", self.arm)
            game_span.set_attribute("player.count", len(self.players))

            # Trace-correlation signal (#1133): capture THIS game span's trace_id and
            # publish it as the game_trace_correlation gauge so the agent can read it
            # off the metric stream and link agent.decision -> this game trace. A
            # non-recording span (sampler dropped it, or telemetry off) reports the
            # all-zero invalid trace id; skip the gauge in that case so the agent never
            # tries to link to an invalid trace. Set to 1 while live; removed at retire.
            span_ctx = game_span.get_span_context()
            if span_ctx.trace_id != 0:
                self.game_trace_id = trace.format_trace_id(span_ctx.trace_id)
                # Carry the root span's span_id too (#1157) so the agent's Link
                # references the actual game-start span (Jaeger uiFind highlight)
                # rather than an all-zero span id under that trace.
                self.game_trace_span_id = trace.format_span_id(span_ctx.span_id)
                # gameplay_phase_span_id (#1195) is empty here: the gameplay_phase
                # span has not opened yet (it opens inside game.run()). Early
                # correlation is preserved via game_trace_span_id; the agent's
                # two-tier fallback uses the game root span until gameplay_phase
                # publishes its id, at which point the gauge is re-emitted with the
                # populated label (publish_gameplay_phase_span_id).
                metrics.game_trace_correlation.labels(
                    game_kind=self.game_kind,
                    game_id=self.game_id,
                    game_trace_id=self.game_trace_id,
                    game_trace_span_id=self.game_trace_span_id,
                    gameplay_phase_span_id=self.gameplay_phase_span_id,
                ).set(1)

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
                        rng_seed=self.rng_seed,
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
                # Thread experiment attribution onto the game so its in-loop
                # set_game_transaction_context carries it for flag evaluation (#975).
                game.experiment_id = self.experiment_id
                game.arm = self.arm
                # Let the game publish its gameplay_phase span_id back to the
                # correlation gauge (#1195) when that span opens, so the agent can
                # re-parent its in-game decision chain under gameplay_phase. The
                # game holds only this callback (not the session), mirroring how the
                # session sets game_kind/experiment_id on the game.
                game.on_gameplay_phase_open = self.publish_gameplay_phase_span_id

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

                # Update lifecycle metrics for THIS session's kind (#775) +
                # experiment attribution (#975). experiment_id/arm are "" for a
                # non-experiment game.
                metrics.active_game.labels(
                    game_kind=self.game_kind,
                    game_id=self.game_id,
                    experiment_id=self.experiment_id,
                    arm=self.arm,
                ).set(0)
                metrics.active_players.labels(
                    game_kind=self.game_kind,
                    game_id=self.game_id,
                    experiment_id=self.experiment_id,
                    arm=self.arm,
                ).set(0)
                if self.is_primary:
                    # players_alive is a single global gauge (not yet per-kind);
                    # only the primary session resets it.
                    metrics.players_alive.set(0)
                if self.game_name:
                    # games_completed_total is a CUMULATIVE counter: it deliberately
                    # does NOT carry experiment_id/arm (a label on a cumulative
                    # counter is one permanent series per experiment, forever).
                    # Per-experiment counts come from the span attributes (#975).
                    metrics.games_completed_total.labels(
                        mode=self.game_name,
                        game_kind=self.game_kind,
                    ).inc()
                if self.game_start_time:
                    duration = time.time() - self.game_start_time
                    metrics.game_duration_seconds.labels(
                        game_kind=self.game_kind,
                        game_id=self.game_id,
                        experiment_id=self.experiment_id,
                        arm=self.arm,
                    ).set(duration)

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
