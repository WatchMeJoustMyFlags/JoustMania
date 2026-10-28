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
  keep working with no changes).
- ``shadow``: agent/test sessions. Each gets its OWN EventBus.

Every session writes its own game_id-labelled game-state gauges (music_tempo,
sensitivity, thresholds, loop Hz, mode) and removes only its own series (FR-041).
"""

import asyncio
import concurrent.futures
import logging
import threading
import time
from collections.abc import Callable
from contextlib import suppress

from opentelemetry import trace
from opentelemetry.context import Context

from lib.feature_flags import eval_game_kind, set_game_session_kind_context
from lib.otel_metrics import get_export_interval_ms
from lib.telemetry import get_tracer
from lib.types import get_game_display_name
from proto import game_coordinator_pb2
from services.game_coordinator import metrics
from services.game_coordinator.event_bus import EventBus
from services.game_coordinator.flag_readiness import await_flag_providers_ready
from services.game_coordinator.game_factory import GameFactory
from services.game_coordinator.grpc_clients import GrpcClientManager

logger = logging.getLogger(__name__)

tracer = get_tracer(__name__)

# UC-035 BR-002: coordinator spans say primary / shadow; the agent says real / shadow (D-T7).
GAME_KIND_PRIMARY = "primary"
GAME_KIND_SHADOW = "shadow"

# games_completed_total{outcome} values: the game.outcome span attribute values
# (UC-020 BR-003, #1328) plus "error" for a session that published game_error
# (no gRPC clients, mode creation failed, game.run() raised). An unknown game
# outcome is recorded as "completed", so the label never grows past these five
# series per (mode, game_kind).
OUTCOME_COMPLETED = "completed"
OUTCOME_FORCE_ENDED = "force_ended"
OUTCOME_ERROR = "error"
GAME_OUTCOMES = frozenset({"winner", "tie", OUTCOME_COMPLETED, OUTCOME_FORCE_ENDED, OUTCOME_ERROR})


def completed_game_outcome(game) -> str:
    """The ``outcome`` label for ``games_completed_total`` (#1427).

    The game's own outcome (``game.outcome`` on its root span). A game that
    reports something unexpected, or whose outcome cannot be read, counts as
    ``completed``. The session itself records ``error`` and ``force_ended``
    for the paths that end without a game outcome (``_run_game_loop_async``).
    """
    if game is None:
        return OUTCOME_COMPLETED
    try:
        outcome = game.game_outcome()
    except Exception as e:
        logger.warning(f"Could not read the game outcome: {e}")
        return OUTCOME_COMPLETED
    return outcome if outcome in GAME_OUTCOMES else OUTCOME_COMPLETED


# How often game_duration_seconds is refreshed while a game is RUNNING (#1305).
DURATION_GAUGE_INTERVAL_S = 1.0

# UC-027 step 3: a force-end waits up to this long for the game loop, in total:
# for the loop to take the mode's force_end (#2149) and then for its thread to finish.
FORCE_END_WAIT_S = 2.0
# How often that wait checks whether the game loop closed or its thread ended.
FORCE_END_POLL_S = 0.05

# Retire keeps a game's zeroed series for this many metric export intervals
# before it removes them (#1311): the agent detects a game's end only from a
# game_active 1 -> 0 datapoint, so the 0 must be exported (at least twice)
# before the series goes.
RETIRE_REMOVAL_EXPORT_INTERVALS = 3


def retire_removal_delay_s() -> float:
    """Seconds between retire's zeroing and the removal of the series (#1311).

    0 when metrics are not exported (not initialized, or disabled for tests):
    there is no datapoint to wait for.
    """
    interval_ms = get_export_interval_ms()
    if not interval_ms:
        return 0.0
    return RETIRE_REMOVAL_EXPORT_INTERVALS * interval_ms / 1000.0


class GameSession:
    """State container + runtime for a single concurrent game.

    The session owns its EventBus, background thread, asyncio loop, gRPC clients,
    and lifecycle state. ``game_kind`` drives the metric labels.
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
        pattern_trigger_policy: bool = False,
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
        # UC-046 BR-018 / UC-040 BR-019 (#2096): the shadow game's pattern controllers
        # press the trigger by themselves, so it honours trigger and hold. The
        # servicer forces False for a real (primary) session, like rng_seed.
        self.pattern_trigger_policy = pattern_trigger_policy

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

        # Mode label of this game's game_current_mode series: the built game's
        # get_game_name(), the same value as the game.mode span attribute. "" until
        # the game is built (publish_game_mode) and again once the series is removed.
        self.game_mode = ""
        # Hex span_id of this session's game_cycle span (#1325, UC-041 BR-004).
        # game_cycle is the child of gameplay_phase the agent parents its in-game
        # spans under (UC-040 BR-013). Empty until base.py opens game_cycle and
        # calls publish_game_cycle_span_id; modes with their own run() never open
        # it, so the agent falls back to gameplay_phase / the game root.
        self.game_cycle_span_id = ""

        # Per-session loop runs in its own thread with its own GrpcClientManager.
        self.clients = GrpcClientManager()
        self.game_thread: threading.Thread | None = None
        # The game thread's event loop, set when the thread starts. The clients
        # above belong to it, so intervention side effects are dispatched onto
        # it (UC-040 BR-018, #1250). None until the thread runs.
        self.game_loop: asyncio.AbstractEventLoop | None = None
        self.game_running = False
        # Called once from the game thread when its game has ended (natural end,
        # error or force end), after the state reached ENDED. The servicer queues
        # the flag-service end here (UC-053 step 7, #1847). Must not block.
        self.on_game_ended: Callable[[GameSession], None] | None = None
        # True once the servicer queued this game's flag-service end; guarded by
        # the servicer's _sessions_lock so the end is queued exactly once (#1847).
        self.flag_service_end_queued = False

        # Protects game_state, current_game, players for this session.
        self._state_lock = threading.Lock()
        # True once a force end has begun (UC-027 BR-003, #1827); guarded by
        # _state_lock. A second force end during the join is refused instead of
        # joining and publishing again. Reset only when the mode's force_end
        # raised, so a later caller can retry.
        self._force_ending = False

        # Retire guard for this session's per-game series (#1311). clear_metrics
        # sets _metrics_retired under _metrics_lock, and every per-game gauge
        # write from the session checks it under the same lock, so a game-thread
        # teardown that runs after retire cannot re-create a removed series.
        self._metrics_lock = threading.Lock()
        self._metrics_retired = False

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

        Final (#1311): retire can run before the game thread's teardown (the
        natural-end sweep sees ENDED before the loop's ``finally:``, or
        ``force_end`` gives up joining). The session is marked retired under
        ``_metrics_lock`` first, so no later session write re-creates a series.

        The game's end stays visible (#1311): the agent detects a game's end
        only from a ``game_active`` 1 -> 0 datapoint. Retire therefore sets
        ``game_active`` / ``game_active_players`` to 0 itself (the teardown may
        not have yet, and will not after retire) and removes the series only
        ``retire_removal_delay_s()`` later, on a timer thread, so the 0 is
        exported first. Retire never blocks on that delay.
        """
        with self._metrics_lock:
            if self._metrics_retired:
                return  # removal already done or scheduled
            self._metrics_retired = True
            self._zero_lifecycle_gauges()
        delay_s = retire_removal_delay_s()
        if delay_s <= 0:
            self._remove_metrics_locked()
            return
        timer = threading.Timer(delay_s, self._remove_metrics_locked)
        timer.daemon = True
        timer.name = f"retire-metrics-{self.game_id}"
        timer.start()

    def _remove_metrics_locked(self) -> None:
        """Remove every per-game series of this retired session (#1018, #1311)."""
        with self._metrics_lock:
            self._remove_metrics()

    def _remove_metrics(self) -> None:
        """Remove every per-game series of this session. Caller holds ``_metrics_lock``."""
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
        # gameplay_phase_span_id, game_cycle_span_id) and was only set when the span
        # was sampled (game_trace_id non-empty), so remove it separately and only
        # then. Remove with the CURRENT phase span ids: each is "" until its span
        # opens and is then updated in lockstep with the gauge re-emit, so this
        # always matches the live series tuple.
        # Per-game mode marker and game-state gauges (FR-041). The game removes
        # its game-state series when run() ends; removing them again here is
        # idempotent and catches a game that never reached its own cleanup.
        self._remove_game_mode()
        metrics.clear_game_state_gauges(self.game_kind, self.game_id)

        if self.game_trace_id:
            with suppress(KeyError, ValueError):
                metrics.game_trace_correlation.remove(*self._correlation_labels())

    def _correlation_labels(self) -> tuple[str, str, str, str, str, str]:
        """The game_trace_correlation label tuple of the live series, in label order."""
        return (
            self.game_kind,
            self.game_id,
            self.game_trace_id,
            self.game_trace_span_id,
            self.gameplay_phase_span_id,
            self.game_cycle_span_id,
        )

    def _set_gauges_unless_retired(self, write: Callable[[], None]) -> None:
        """Run ``write`` (per-game gauge writes) unless this session is retired (#1311)."""
        with self._metrics_lock:
            if not self._metrics_retired:
                write()

    def _set_trace_correlation(self) -> None:
        """Set the game_trace_correlation series for the current label tuple to 1."""
        metrics.game_trace_correlation.labels(
            game_kind=self.game_kind,
            game_id=self.game_id,
            game_trace_id=self.game_trace_id,
            game_trace_span_id=self.game_trace_span_id,
            gameplay_phase_span_id=self.gameplay_phase_span_id,
            game_cycle_span_id=self.game_cycle_span_id,
        ).set(1)

    def _republish_trace_correlation(self, attr: str, span_id: str) -> None:
        """Swap the live correlation series for one carrying a newly opened span's id.

        attr names the session field (and gauge label) to update. The previous
        series (with the old, usually empty, value) is removed first so exactly one
        series per live game remains, and clear_metrics removes the live tuple at
        retire. No-op when the game span was unsampled (no gauge was ever set), the
        id is empty, or it is unchanged.
        """
        if not self.game_trace_id or not span_id:
            return
        with self._metrics_lock:
            if self._metrics_retired or getattr(self, attr) == span_id:
                return
            with suppress(KeyError, ValueError):
                metrics.game_trace_correlation.remove(*self._correlation_labels())
            setattr(self, attr, span_id)
            self._set_trace_correlation()

    def publish_game_cycle_span_id(self, game_cycle_span_id: str) -> None:
        """Re-emit the correlation gauge carrying the game_cycle span_id (#1325).

        Called by the game (base.py) right after it opens game_cycle. The agent
        parents its in-game spans under game_cycle (UC-040 BR-013); until this is
        published it falls back to gameplay_phase, then the game root.
        """
        self._republish_trace_correlation("game_cycle_span_id", game_cycle_span_id)

    def publish_game_mode(self, mode: str) -> None:
        """Set this game's game_current_mode series to 1 (FR-041).

        ``mode`` is the built game's ``get_game_name()`` ("FFA", "Zombie", ...),
        the same vocabulary as the game.mode span attribute, so the agent sees one
        consistent mode (not the menu enum name in ``game_name``). One series per
        game (mode, game_kind, game_id); removed when the game loop ends and again
        at retire.
        """
        with self._metrics_lock:
            if self._metrics_retired:
                return
            self.game_mode = mode
            metrics.current_game_mode.labels(
                mode=mode,
                game_kind=self.game_kind,
                game_id=self.game_id,
            ).set(1)

    def _remove_game_mode(self) -> None:
        """Remove this game's game_current_mode series (idempotent)."""
        if not self.game_mode:
            return
        with suppress(KeyError, ValueError):
            metrics.current_game_mode.remove(self.game_mode, self.game_kind, self.game_id)
        self.game_mode = ""

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
        self._republish_trace_correlation("gameplay_phase_span_id", gameplay_phase_span_id)

    def _set_duration_gauge(self) -> None:
        """Set game_duration_seconds to the seconds since this game was started.

        Same label tuple (game_kind, game_id, experiment_id, arm) as the other
        per-game lifecycle gauges, so it is one series per live game and is
        removed at retire by ``clear_metrics`` (bounded cardinality).
        """
        if not self.game_start_time:
            return
        self._set_gauges_unless_retired(
            lambda: metrics.game_duration_seconds.labels(
                game_kind=self.game_kind,
                game_id=self.game_id,
                experiment_id=self.experiment_id,
                arm=self.arm,
            ).set(time.time() - self.game_start_time)
        )

    async def _publish_duration_while_running(self) -> None:
        """Refresh game_duration_seconds every DURATION_GAUGE_INTERVAL_S while RUNNING (#1305).

        Before #1305 the gauge was only set at game end, so the agent saw an
        elapsed time of 0 for the whole game. Runs for real and shadow sessions
        alike; cancelled by the game loop when ``game.run()`` returns.
        """
        warned = False
        while self.game_running:
            if self.game_state == game_coordinator_pb2.GameState.RUNNING:
                try:
                    self._set_duration_gauge()
                except Exception as e:
                    # A metrics failure must not stop the ticker or fail the game;
                    # warn once per game, then keep trying quietly.
                    log = logger.debug if warned else logger.warning
                    log(f"Could not update game_duration_seconds for game {self.game_id}: {e}")
                    warned = True
            await asyncio.sleep(DURATION_GAUGE_INTERVAL_S)

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
        self.game_loop = loop
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
        return eval_game_kind(self.game_kind)

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
                self._set_gauges_unless_retired(self._set_trace_correlation)

            # The built game (None until GameFactory created it) and the outcome the
            # session decides itself (error, or force_ended before the game was
            # built); both feed games_completed_total{outcome}.
            game = None
            session_outcome: str | None = None
            try:
                # Check if gRPC clients are available
                if not self.clients.is_connected:
                    error_msg = "gRPC clients not initialized - ControllerManager service must be running"
                    logger.error(error_msg)
                    with self._state_lock:
                        self.game_state = game_coordinator_pb2.GameState.ENDED
                    session_outcome = OUTCOME_ERROR
                    await self.event_bus.publish("game_error", {"error": error_msg})
                    await self.clients.close()
                    return

                # UC-010 A5 / BR-004 (#1329): no game reads its start flags from a
                # provider that is not READY yet: wait (bounded), else record the
                # code-default fallback on this root span.
                admitted = self.game_running
                await await_flag_providers_ready(game_span)

                # A force-end during the wait (game_running flipped to False) retired
                # this session: do not create and run an orphan game behind its back
                # (#1329).
                if admitted and not self.game_running:
                    logger.info(f"Game {self.game_id} was force-ended while waiting for flag providers")
                    session_outcome = OUTCOME_FORCE_ENDED
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
                    session_outcome = OUTCOME_ERROR
                    await self.event_bus.publish("game_error", {"error": error_msg})
                    await self.clients.close()
                    return

                # Tell the game which session it belongs to so its end-of-game
                # analytics cleanup and game-state gauges are per-session (#775,
                # FR-041).
                game.game_kind = self.game_kind
                # Thread experiment attribution onto the game so its in-loop
                # set_game_transaction_context carries it for flag evaluation (#975).
                game.experiment_id = self.experiment_id
                game.arm = self.arm
                game.pattern_trigger_policy = self.pattern_trigger_policy
                # Let the game publish its gameplay_phase span_id back to the
                # correlation gauge (#1195) when that span opens, so the agent can
                # re-parent its in-game decision chain under gameplay_phase. The
                # game holds only this callback (not the session), mirroring how the
                # session sets game_kind/experiment_id on the game.
                game.on_gameplay_phase_open = self.publish_gameplay_phase_span_id
                # Same for game_cycle (#1325): the agent's preferred parent.
                game.on_game_cycle_open = self.publish_game_cycle_span_id

                # Store reference and run game. The duration ticker shares this
                # session's event loop (no extra thread/loop) and keeps
                # game_duration_seconds live while RUNNING (#1305); it is stopped
                # before the final value is set below so a late tick cannot
                # overwrite it.
                # Publish the game atomically with force_end's check (#2025 S1): a
                # force-end that already ran found no game to stop and reported
                # ENDED, so this game must not run; one that runs later finds it.
                with self._state_lock:
                    force_ended_while_building = self._force_ending
                    if not force_ended_while_building:
                        self.current_game = game
                if force_ended_while_building:
                    logger.info(f"Game {self.game_id} was force-ended while it was being created")
                    session_outcome = OUTCOME_FORCE_ENDED
                    await self.clients.close()
                    return
                self.publish_game_mode(game.get_game_name())
                duration_ticker = asyncio.create_task(self._publish_duration_while_running())
                try:
                    await game.run()
                finally:
                    duration_ticker.cancel()
                    try:
                        await duration_ticker
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        # Telemetry must never turn a finished game into game_error.
                        logger.warning(f"Duration ticker for game {self.game_id} failed: {e}")
                logger.info(f"{self.game_name} game completed")

            except Exception as e:
                logger.error(f"Game loop error: {e}", exc_info=True)
                with self._state_lock:
                    self.game_state = game_coordinator_pb2.GameState.ENDED
                session_outcome = OUTCOME_ERROR
                # A base-run mode already published it before re-raising (#2025 S2).
                if getattr(game, "game_error_published", False) is not True:
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

                # Report the end now, not at the next start's sweep or the next
                # reconcile (UC-053 step 7, #1847). Outside _state_lock.
                if self.on_game_ended is not None:
                    try:
                        self.on_game_ended(self)
                    except Exception as e:
                        logger.warning(f"Game-ended hook failed for game {self.game_id}: {e}")

                # The game is over: its mode series goes now, not at retire, so
                # it never reads 1 between games (retire removes it again).
                with self._metrics_lock:
                    self._remove_game_mode()

                # Update lifecycle metrics for THIS session's kind (#775) +
                # experiment attribution (#975). experiment_id/arm are "" for a
                # non-experiment game. Skipped when retire already removed the
                # series (#1311): the zero would re-create a series nothing
                # removes again.
                self._set_gauges_unless_retired(self._zero_lifecycle_gauges)
                if self.is_primary:
                    # players_alive is a single global gauge (not yet per-kind);
                    # only the primary session resets it.
                    metrics.players_alive.set(0)
                if self.game_name:
                    # games_completed_total is a CUMULATIVE counter: it deliberately
                    # does NOT carry experiment_id/arm (a label on a cumulative
                    # counter is one permanent series per experiment, forever).
                    # Per-experiment counts come from the span attributes (#975).
                    # outcome is one of the five GAME_OUTCOMES (#1427).
                    metrics.games_completed_total.labels(
                        mode=self.game_name,
                        game_kind=self.game_kind,
                        outcome=session_outcome or completed_game_outcome(game),
                    ).inc()
                # Final value: the gauge holds the whole game's duration at end.
                try:
                    self._set_duration_gauge()
                except Exception as e:
                    logger.warning(f"Could not set final game_duration_seconds for game {self.game_id}: {e}")

                # Cleanup channels
                await self.clients.close()
                logger.info(f"Closed gRPC channels for game {self.game_id}")

    def _zero_lifecycle_gauges(self) -> None:
        """Set this game's game_active and game_active_players series to 0."""
        labels = {
            "game_kind": self.game_kind,
            "game_id": self.game_id,
            "experiment_id": self.experiment_id,
            "arm": self.arm,
        }
        metrics.active_game.labels(**labels).set(0)
        metrics.active_players.labels(**labels).set(0)

    async def force_end(self, reason: str) -> tuple[bool, str]:
        """Force end this session's game.

        Stops the loop, calls ``force_end()`` on the live game, joins the thread,
        transitions to ENDED, and publishes ``game_force_ended``. No-ops safely
        when this session is not in a startable/running state.

        Runs at most once per session (UC-027 BR-003, #1827): a call while a
        force end is under way answers ``Game is already ending``. When the
        mode's ``force_end`` raises, the guard is reset so a later call can
        retry; a cancel during the join keeps it (the game thread's own end
        sets ENDED).
        """
        with self._state_lock:
            if self.game_state not in [
                game_coordinator_pb2.GameState.STARTING,
                game_coordinator_pb2.GameState.RUNNING,
            ]:
                return False, "No game in progress"
            if self._force_ending:
                return False, "Game is already ending"

            self._force_ending = True
            self.game_running = False
            current_game = self.current_game
            game_thread = self.game_thread

        deadline = time.monotonic() + FORCE_END_WAIT_S
        if current_game and hasattr(current_game, "force_end"):
            try:
                await self._force_end_on_game_loop(current_game, reason, FORCE_END_WAIT_S)
            except Exception:
                with self._state_lock:
                    self._force_ending = False
                raise

        # Join in executor to avoid blocking the gRPC server.
        if game_thread and game_thread.is_alive():
            loop = asyncio.get_running_loop()
            join_timeout = max(0.0, deadline - time.monotonic())
            await loop.run_in_executor(None, lambda: game_thread.join(timeout=join_timeout))

        with self._state_lock:
            self.game_state = game_coordinator_pb2.GameState.ENDED

        await self.event_bus.publish(
            "game_force_ended",
            {"reason": reason, "game_id": self.game_id},
        )

        logger.info(f"Force ended game {self.game_id}: {reason}")
        return True, ""

    async def _force_end_on_game_loop(self, game, reason: str, timeout_s: float) -> None:
        """Apply the mode's ``force_end`` on the game's own loop (UC-027 step 3, #2149).

        The game loop is the only thread that reads ``running`` and
        ``force_end_reason`` (the kill gate, the win check), so applying the
        force-end there makes it atomic with the kill path: it lands before a
        kill's gate check (the kill is declined) or after its bookkeeping, never
        in between. Only the force-end pays for this; the frame path is unchanged.

        Applied directly only when provably no game code runs any more: the loop
        is absent, closed or not running, or (while waiting) the loop closed or
        the game thread ended with the callback still queued. A loop that is alive
        but does not take it within ``timeout_s`` keeps it queued and applies it
        when it runs again; the caller goes on with the join and the end. A claim
        makes sure it is applied once. The mode's exception reaches a caller that
        still waits, and is logged otherwise.
        """
        game_loop = self.game_loop
        game_thread = self.game_thread
        claim_lock = threading.Lock()
        claimed = False

        def claim() -> bool:
            nonlocal claimed
            with claim_lock:
                if claimed:
                    return False
                claimed = True
                return True

        def apply_directly() -> None:
            if claim():
                game.force_end(reason)

        try:
            servicer_loop = asyncio.get_running_loop()
        except RuntimeError:
            servicer_loop = None
        if game_loop is None or game_loop is servicer_loop or game_loop.is_closed() or not game_loop.is_running():
            apply_directly()
            return

        done: concurrent.futures.Future[None] = concurrent.futures.Future()

        def apply_on_game_loop() -> None:
            if not claim():
                return
            try:
                game.force_end(reason)
            except BaseException as exc:
                try:
                    done.set_exception(exc)  # handed to the caller, not lost on the loop
                except concurrent.futures.InvalidStateError:
                    logger.error(
                        f"Mode force_end of {self.game_id} failed after the caller stopped waiting", exc_info=exc
                    )
            else:
                with suppress(concurrent.futures.InvalidStateError):
                    done.set_result(None)

        def game_loop_gone() -> bool:
            return game_loop.is_closed() or (game_thread is not None and not game_thread.is_alive())

        try:
            game_loop.call_soon_threadsafe(apply_on_game_loop)
        except RuntimeError:  # closed after the check above
            apply_directly()
            return

        waiter = asyncio.wrap_future(done)
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if done.done():  # taken in the last moment: surface its result
                        done.result()
                        return
                    logger.warning(
                        f"Game loop of {self.game_id} did not take the force-end within {timeout_s}s; "
                        "it stays queued and applies when the loop runs again"
                    )
                    return
                finished, _ = await asyncio.wait({waiter}, timeout=min(FORCE_END_POLL_S, remaining))
                if finished:
                    waiter.result()  # the mode's exception, if any
                    return
                if game_loop_gone():
                    # The queued callback will never run: no game code runs any more.
                    apply_directly()
                    return
        finally:
            # Stop waiting now, not on a later loop turn: a late failure of the
            # queued force_end is then logged above, never routed to a dead waiter.
            done.cancel()
            waiter.cancel()

    async def join_thread(self, join_timeout_s: float = 2.0) -> None:
        """Join this session's game thread (used on servicer shutdown)."""
        with self._state_lock:
            self.game_running = False
            game_thread = self.game_thread

        if game_thread and game_thread.is_alive():
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: game_thread.join(timeout=join_timeout_s))

        await self.clients.close()
