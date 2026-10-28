"""
GameCoordinator gRPC Servicer for JoustMania

Manages game lifecycle:
- Start games with player configurations
- Monitor game state
- Force end games
- Stream game events (deaths, scoring, game end)

Multi-session (#775): the servicer holds ``dict[game_id, GameSession]`` instead
of a single game's fields, so multiple games can run concurrently. A persistent
PRIMARY EventBus (created here, never destroyed) preserves the legacy
"subscribe before any game exists" semantics. Zero-arg ``StreamGameEvents``
subscribers attach to the primary bus exactly as before; a ``StreamGameEvents``
call WITH start_config subscribes to the bus of the session it just created.

Shadow game governance (#837): primary is NO LONGER first-come-first-served.
Only a ``GAME_ORIGIN_MENU`` start (the real game) may claim the primary slot +
persistent primary bus; UNSPECIFIED/AGENT starts are ALWAYS shadow sessions
(own EventBus), even when the system is idle. A real game start preempts
(force-ends) running shadows so it gets all resources, and — when
``shadow_policy=block`` (prod default) — a shadow start is rejected while a real
game is live. ``ForceEndGame`` / ``GetGameState`` with an empty game_id operate
on the primary session (game_id routing is #776).

Concurrent shadow games (#1018, option b): the single-game guard ("Game already
in progress") applies to REAL/primary games ONLY. A REAL game is strictly
one-at-a-time (the primary slot; a second menu start is rejected). SHADOW games,
being headless + game_id-isolated (each with its own EventBus/state), run
CONCURRENTLY up to ``GAME_MAX_SHADOW_GAMES`` (default 4) — this is what realizes
the agent registry's K×N capacity (effective concurrency was 1 under #998's
backpressure). The real-vs-shadow interaction policy is unchanged and simple:
the real game ALWAYS takes precedence — an incoming real game preempts ALL live
shadows (#837), and with ``shadow_policy=block`` (prod default) a shadow start is
refused while a real game is live. The only thing #1018 changes is that shadows
no longer trip the single-game guard among THEMSELVES. ``GAME_MAX_CONCURRENT_GAMES``
(default 8) is the hard total ceiling on primary + shadow sessions — a runaway
safety net, not the single-game guard.
"""

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Awaitable, Callable

from opentelemetry import trace

from lib.telemetry import get_tracer
from lib.types import GameEvent, Games
from proto import game_coordinator_pb2, game_coordinator_pb2_grpc
from services.game_coordinator import metrics
from services.game_coordinator.difficulty_handlers import register_difficulty_handlers
from services.game_coordinator.event_bus import EventBus
from services.game_coordinator.flag_service_client import FlagServiceLifecycle, GameRecord
from services.game_coordinator.game_session import GAME_KIND_PRIMARY, GAME_KIND_SHADOW, GameSession
from services.game_coordinator.interventions import InterventionManager, SessionView
from services.game_coordinator.lifecycle_handlers import register_lifecycle_handlers
from services.game_coordinator.tempo_schedule import TempoScheduleManager

logger = logging.getLogger(__name__)

# Lazy telemetry initialization - defers OTLP setup until first span
tracer = get_tracer(__name__)

# Total concurrency cap (#775). The hard ceiling on TOTAL live sessions
# (primary + every shadow) the coordinator will register at once — a runaway
# safety net, NOT the single-game guard. Default 8 leaves room for one real
# game plus several concurrent shadow games; tune via GAME_MAX_CONCURRENT_GAMES.
DEFAULT_MAX_CONCURRENT_GAMES = 8


def injected_parent_span_context(span):
    """The span context the caller injected as traceparent (#1182, #2223), or None.

    With ``grpc_rpc_spans`` on, the current span is the server interceptor's SERVER
    span (lib/grpc_tracing.py), a CHILD of the injected context: the injected
    context is its remote parent, and a server span with no remote parent means no
    traceparent came in. With it off, the current span is the extracted context
    itself (a non-recording span), so its own context is the injected one.
    """
    if span is None:
        return None
    if hasattr(span, "parent"):
        parent = span.parent
        if parent is not None and parent.is_valid and parent.is_remote:
            return parent
        return None
    return span.get_span_context()


def _max_concurrent_games() -> int:
    """Read the TOTAL concurrent-games ceiling from the ``game`` flagd domain.

    Bounds primary + shadow sessions together so a misconfigured agent cannot
    register unbounded sessions. The single-game guard for REAL games and the
    shadow concurrency cap are enforced separately (see ``_admit_session_locked``).

    Read from the ``max_concurrent_games`` game flag with the typed int getter
    (#1215). Fail-open: on any error (flagd unreachable/undefined at startup, the
    #1127 TYPE_MISMATCH trap, etc.) ``read_int_flag`` returns the hardcoded
    ``DEFAULT_MAX_CONCURRENT_GAMES`` default so the coordinator never crashes."""
    from lib.feature_flags import read_int_flag

    value = read_int_flag("game", "max_concurrent_games", DEFAULT_MAX_CONCURRENT_GAMES)
    return max(1, value)


# Concurrent-shadow cap (#1018, option b). Shadow games are headless and
# game_id-isolated (own EventBus/state), so N may run AT THE SAME TIME — this is
# what realizes the registry's K×N capacity (effective concurrency was 1 under
# #998's backpressure). This caps how many shadow sessions run concurrently,
# independent of the REAL game's single-game guard. Default 4 is a sane >1 value
# for a single-interpreter coordinator on Pi 5-class hardware (well under the
# agent's compiled 20-game registry ceiling); tune via
# GAME_MAX_SHADOW_GAMES. Always clamped to the total ceiling above.
DEFAULT_MAX_SHADOW_GAMES = 4


def _max_shadow_games() -> int:
    """Read the concurrent-shadow cap from the ``game`` flagd domain (default 4).

    This is the number of shadow sessions allowed to run CONCURRENTLY. It is
    independent of the real-game single-game guard (a real game preempts shadows
    and owns the primary slot). Clamped to the total ceiling so it can never
    exceed ``_max_concurrent_games()``.

    Read from the ``max_shadow_games`` game flag with the typed int getter
    (#1215). Fail-open: on any error ``read_int_flag`` returns the hardcoded
    ``DEFAULT_MAX_SHADOW_GAMES`` default. The clamp to ``_max_concurrent_games()``
    is preserved unchanged."""
    from lib.feature_flags import read_int_flag

    value = read_int_flag("game", "max_shadow_games", DEFAULT_MAX_SHADOW_GAMES)
    return max(1, min(value, _max_concurrent_games()))


# Shadow game resource policy (#837). ``block`` (prod default) rejects shadow
# starts while a real game is live; ``allow`` (CI) permits concurrent shadow +
# real games (the #779 isolation test + #826 parallel batches need this).
DEFAULT_SHADOW_POLICY = "block"

# The force-end reason of a shadow preempted by a real start (UC-043 A2).
PREEMPTED_REASON = "preempted_by_real_game"


def _shadow_policy() -> str:
    """Read the shadow-game policy from the ``game`` flagd domain (#837).

    Live per-call read so a flag flip takes effect immediately. Any unknown
    value falls back to the safe default (``block``). Kept module-level so unit
    tests can patch it (mirroring how flag-dependent behavior is tested
    elsewhere)."""
    from lib.feature_flags import read_string_flag

    value = read_string_flag("game", "shadow_policy", DEFAULT_SHADOW_POLICY)
    return value if value in ("block", "allow") else DEFAULT_SHADOW_POLICY


class GameCoordinatorServicer(game_coordinator_pb2_grpc.GameCoordinatorServiceServicer):
    """
    GameCoordinator gRPC servicer.

    Manages the lifecycle of one or more concurrent games via a session
    registry. See the module docstring for the primary/shadow bus design.
    """

    def __init__(self):
        """Initialize game coordinator."""
        # Session registry: game_id -> GameSession. Guarded by _sessions_lock for
        # dict mutation (add/remove); each session has its own state lock.
        self.sessions: dict[str, GameSession] = {}
        self._sessions_lock = threading.Lock()
        # game_id of the active primary session, or None when no primary is live.
        self._primary_game_id: str | None = None
        # Session ends under way, game_id -> (session, task) (#1816, #1827): a
        # force end (ForceEndGame, end_game) or a preemption. Each runs as its own
        # task so a cancelled caller cannot stop it halfway; the dict holds the
        # strong reference and lets a second caller wait for the end under way
        # instead of ending the session again. Touched only on the servicer loop.
        self._session_endings: dict[str, tuple[GameSession, asyncio.Task[tuple[bool, str]]]] = {}

        # Persistent PRIMARY EventBus — created once, NEVER destroyed. Zero-arg
        # StreamGameEvents subscribers (the menu) attach here even before any
        # game exists, preserving legacy subscribe-before-start semantics. Its
        # state-sync callback routes to whichever session owns the primary slot.
        self.primary_event_bus = EventBus(state_sync_callback=self._on_primary_event_state_sync)

        # Random game history
        self.random_history: list[str] = []

        # Agent intervention manager (#730): subscribes to the flagd
        # `interventions` domain and applies agent interventions to the live
        # games via the enforcement chain. Per-session evaluation (#838):
        # get_intervention_sessions() returns one SessionView per LIVE game
        # (primary + every shadow), each carrying its game_id (-> gameId context),
        # its running BaseGameMode, and its own EventBus publish, so a
        # gameId-targeted intervention applies only to the matching session and
        # publishes on that session's stream. An un-targeted intervention (the
        # agent writes no gameId targeting) resolves to the same value for every
        # live session, primary and shadow alike (decide-contract §11, #1269).
        self.intervention_manager = InterventionManager(
            event_publisher=self.primary_event_bus.publish,
            get_sessions=self.get_intervention_sessions,
            battery_provider=self._battery_pct_for_serial,
            # end_game ends exactly the session it was evaluated for, never
            # "the current game" (UC-040 BR-011, #1310).
            end_game_fn=self._force_end_session,
        )
        # PR E: register the ambient/session handlers (audio cue, volume,
        # controller effect, end game).
        self.intervention_manager.register_ambient_handlers()
        # Register difficulty intervention handlers (#730 PR C): music tempo
        # override, global sensitivity override, per-player sensitivity factor.
        register_difficulty_handlers(self.intervention_manager)
        # Register shield + lifecycle intervention handlers (#730 PR D):
        # shield_seconds (per-player grace shield), eliminate_player, revive_player.
        register_lifecycle_handlers(self.intervention_manager)
        self.intervention_manager.start()

        # Tempo-schedule SOURCE listener (#1109): subscribes to flagd's
        # PROVIDER_CONFIGURATION_CHANGED event and re-reads `game.tempo_schedule_mode`
        # for every live session's gameId, atomically swapping each game's
        # `tempo_schedule_mode` so the pacing SOURCE (rule | agent) switches LIVE
        # mid-game. Reuses the same per-session seam as the InterventionManager.
        self.tempo_schedule_manager = TempoScheduleManager(get_sessions=self.get_intervention_sessions)
        self.tempo_schedule_manager.start()

        # Flag service game lifecycle (FR-135, #1410): register each game before
        # it runs (bounded 200 ms, outside _sessions_lock), report its end, and
        # reconcile this instance's live games at start and after a failure.
        # Disabled when FLAG_SERVICE_URL is unset.
        self.flag_service = FlagServiceLifecycle.from_env(self._flag_service_live_games)
        self.flag_service.start()

        logger.info("GameCoordinator initialized")

    # ------------------------------------------------------------------
    # Primary-session accessors (ForceEndGame / GetGameState / interventions
    # all operate on the primary session until game_id routing lands in #776).
    # ------------------------------------------------------------------

    @property
    def event_bus(self) -> EventBus:
        """Primary EventBus (back-compat alias used by tests and interventions)."""
        return self.primary_event_bus

    def _get_primary_session(self) -> GameSession | None:
        """Return the active primary session, or None."""
        with self._sessions_lock:
            if self._primary_game_id is None:
                return None
            return self.sessions.get(self._primary_game_id)

    def _resolve_session(self, game_id: str) -> GameSession | None:
        """Resolve a request game_id to a session (#776).

        Empty game_id => the active primary session (exact legacy semantics).
        A non-empty game_id => that specific session, or None if unknown.
        """
        if not game_id:
            return self._get_primary_session()
        with self._sessions_lock:
            return self.sessions.get(game_id)

    @property
    def current_game(self):
        """Primary session's running game instance (back-compat for tests)."""
        session = self._get_primary_session()
        return session.current_game if session else None

    @property
    def game_state(self):
        """Primary session's state (back-compat). IDLE when no primary."""
        session = self._get_primary_session()
        return session.game_state if session else game_coordinator_pb2.GameState.IDLE

    @property
    def game_id(self):
        """Primary session's game_id (back-compat), or None."""
        return self._primary_game_id

    def get_intervention_sessions(self) -> list["SessionView"]:
        """Snapshot the live sessions for the InterventionManager (#838).

        Returns one ``SessionView`` per session whose game is constructed and
        running, carrying:
          * ``game_id`` — stamped into the ``gameId`` EvaluationContext so flagd
            targeting rules can scope an intervention to one game;
          * ``game`` — the live BaseGameMode handlers mutate;
          * ``publish`` — THAT session's EventBus publish, so effects/blocked
            events land on the owning game's stream (and reach its subscribers /
            the agent runner).

        Sessions with no constructed game yet (STARTING before the game thread
        built the instance) are skipped — there is nothing to apply to. This
        includes the primary and every shadow session uniformly, so a
        gameId-targeted intervention reaches the matching session and an
        un-targeted one (baseline default) resolves to the same value for every live session.
        """
        views: list[SessionView] = []
        with self._sessions_lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            game = session.current_game
            if game is None:
                continue
            views.append(
                SessionView(
                    game_id=session.game_id,
                    game=game,
                    publish=session.event_bus.publish,
                    is_real=session.is_primary,
                    # Handler side effects run on this game's own loop, where
                    # its gRPC clients live (UC-040 BR-018, #1250).
                    loop=session.game_loop,
                )
            )
        return views

    def _battery_pct_for_serial(self, serial: str) -> float | None:
        """Return the live battery percentage (0-100) for a controller serial.

        Backs the InterventionManager battery guard (#798). The authoritative
        in-process source is the running game's per-player ``battery_pct``, which
        the game updates from each ``GameplayData.battery`` frame (the same signal
        that feeds the controller manager's ``controller_battery_pct`` metric).

        Multi-session note (#775/#793/#838): interventions are now evaluated per
        live session, so a serial may belong to any live game (primary OR a
        shadow). This searches every live session's players for the serial and
        returns the first match's battery, so a gameId-targeted player
        intervention is battery-gated against the controller's actual game.

        Returns None when no game holds the serial, or no battery frame has been
        seen yet — None means "unknown", so the guard does not block (missing
        data must never block, per #798).
        """
        with self._sessions_lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            game = session.current_game
            players = getattr(game, "players", None) if game is not None else None
            if not isinstance(players, dict):
                continue
            player = players.get(serial)
            if player is not None:
                return getattr(player, "battery_pct", None)
        return None

    def _on_primary_event_state_sync(self, event_type: str):
        """State-sync callback for the persistent primary bus.

        Routes to the primary session's own state-sync so the primary session's
        ``game_state`` tracks lifecycle events. No-ops when no primary is live.
        """
        session = self._get_primary_session()
        if session is not None:
            session.on_event_state_sync(event_type)

    # ------------------------------------------------------------------
    # Start path
    # ------------------------------------------------------------------

    @staticmethod
    def _is_real_origin(config) -> bool:
        """A start is the REAL game only when the menu marked it (#837).

        Only ``GAME_ORIGIN_MENU`` may claim the primary slot. UNSPECIFIED (origin
        not set by the caller) and AGENT are ALWAYS shadow, even when no primary
        is live — this kills the first-come-first-served hazard where an idle-time
        agent game could grab primary and block real players.
        """
        return getattr(config, "origin", game_coordinator_pb2.GAME_ORIGIN_UNSPECIFIED) == (
            game_coordinator_pb2.GAME_ORIGIN_MENU
        )

    def _live_shadow_sessions(self) -> list["GameSession"]:
        """Snapshot the shadow sessions in a STARTING/RUNNING state (#837)."""
        with self._sessions_lock:
            return [
                sess
                for sess in self.sessions.values()
                if sess.game_kind == GAME_KIND_SHADOW
                and sess.game_state
                in (
                    game_coordinator_pb2.GameState.STARTING,
                    game_coordinator_pb2.GameState.RUNNING,
                )
            ]

    def _has_live_real_game(self) -> bool:
        """True when a real (menu-origin/primary) game is STARTING or RUNNING (#837).

        The primary slot is reserved for the real game, so a live primary session
        is exactly "a real game is running". ENDED sessions don't count.
        """
        session = self._get_primary_session()
        if session is None:
            return False
        return session.game_state in (
            game_coordinator_pb2.GameState.STARTING,
            game_coordinator_pb2.GameState.RUNNING,
        )

    async def _preempt_shadow_sessions(self) -> int:
        """Force-end every live shadow session so a real game gets all resources.

        Publishes ``game_force_ended`` with reason ``preempted_by_real_game`` on
        each shadow's bus (so agent runners learn why their game vanished), then
        retires the session to free its concurrency slot. Returns the count of
        shadows ended (by this pass or an end already under way). Logged loudly.
        Applies regardless of ``shadow_policy`` — the real game always wins
        resources (#837).

        Each shadow's preemption runs as its session's end task, awaited
        through ``asyncio.shield`` (#1816, #1827): a cancel of the real start
        propagates from here, but a preemption that has begun still completes
        (force end, ``game_force_ended``, retire, count). Shadows this pass has
        not reached keep running. A later pass, or a force end, already under
        way for a shadow is waited for instead of ending it again; only an end
        started by a preemption counts in ``shadow_games_preempted_total``. An
        end task cancelled itself (only by event-loop teardown) raises
        ``CancelledError`` here even though this caller was not cancelled.
        """
        shadows = self._live_shadow_sessions()
        if not shadows:
            return 0

        logger.warning(
            "Preempting %d live shadow game(s) for an incoming real game: %s",
            len(shadows),
            [s.game_id for s in shadows],
        )
        preempted = 0
        for shadow in shadows:
            success, _error = await asyncio.shield(self._session_ending_task(shadow, PREEMPTED_REASON))
            if success:
                preempted += 1
        return preempted

    def _session_ending_task(self, session: GameSession, reason: str) -> "asyncio.Task[tuple[bool, str]]":
        """The end task of ``session``: the one under way, or a new one (#1816, #1827).

        No await between the lookup and the insert, so two callers on the
        servicer loop always share one task. Keyed by game_id and checked by
        identity, so a recycled id never joins a stale task.
        """
        game_id = session.game_id
        pending = self._session_endings.get(game_id)
        if pending is not None and pending[0] is session:
            logger.info(
                "End of game %s already under way; reason %r joins it and is not applied",
                game_id,
                reason,
            )
            return pending[1]

        task = asyncio.ensure_future(self._end_session(session, reason))
        self._session_endings[game_id] = (session, task)

        def _forget(done: "asyncio.Task[tuple[bool, str]]") -> None:
            current = self._session_endings.get(game_id)
            if current is not None and current[1] is done:
                del self._session_endings[game_id]

        task.add_done_callback(_forget)
        return task

    async def _end_session(self, session: GameSession, reason: str) -> tuple[bool, str]:
        """Force-end ``session``, then retire it if still registered (UC-027 steps 3-4).

        Runs as its own task (#1816, #1827), so it never raises an ``Exception``:
        an error is logged and answered as ``(False, error)``, which keeps an
        orphaned task (its caller cancelled) from leaving an unretrieved
        exception. A no-op end (``No game in progress``, UC-027 A2) logs no error
        unless it was a preemption. The preemption metric is counted here, once
        per preempted session.
        """
        preemption = reason == PREEMPTED_REASON
        try:
            success, error = await session.force_end(reason)
        except Exception as e:
            if preemption:
                logger.error("Failed to preempt shadow game %s", session.game_id, exc_info=True)
            else:
                logger.error("Failed to force-end game %s", session.game_id, exc_info=True)
            return False, str(e)
        if not success:
            if preemption:
                logger.error("Failed to preempt shadow game %s: %s", session.game_id, error)
            return False, error
        with self._sessions_lock:
            # The ENDED sweep may have retired it during the join.
            still_registered = self.sessions.get(session.game_id) is session
        if still_registered:
            self._retire_session(session.game_id)
        if preemption:
            metrics.shadow_games_preempted_total.inc()
            logger.warning("Preempted shadow game %s for real game", session.game_id)
        return True, ""

    def _admit_session_locked(self, config, is_real: bool, parent_span):
        """Sweep + classify + cap-check + register a session under the lock.

        Returns ``((session, game_kind, game_id), cap_full)`` where the first
        element is ``None`` when a cap rejected the start (``cap_full=True``).
        Extracted from ``_start_game_from_config`` so the real-game admission
        retry (#837) can re-run it after a second preemption pass.

        Single-game guard scoping (#1018, option b): the guard that rejects a
        second concurrent start with "Game already in progress" now applies to
        REAL/primary games ONLY. A REAL game is one-at-a-time (the primary slot);
        SHADOW games run CONCURRENTLY up to ``_max_shadow_games()`` because they
        are headless + game_id-isolated (own EventBus/state). The total ceiling
        ``_max_concurrent_games()`` bounds primary + shadow together as a runaway
        safety net.
        """
        with self._sessions_lock:
            # Retire sessions whose games already ENDED (#775). A natural
            # game end (win condition) leaves the session registered so
            # GetGameState keeps reporting ENDED until the next start —
            # mirroring the legacy single-game behavior — but an ended
            # session must not occupy a concurrency slot or pin the
            # primary role. (Force-end retires eagerly; this covers the
            # natural-end path.)
            for ended_id in [
                gid for gid, sess in self.sessions.items() if sess.game_state == game_coordinator_pb2.GameState.ENDED
            ]:
                ended_session = self.sessions.pop(ended_id, None)
                self.intervention_manager.forget_session(ended_id)
                # Queued, not sent: no network call under the lock. Usually the
                # game thread already queued it at its end (#1847).
                self._queue_flag_service_end_locked(ended_id, ended_session)
                if ended_session is not None:
                    # Mirror _retire_session: drop this session's game_id-labeled
                    # gauge series so naturally-ended shadows don't leak series
                    # (#1018). Idempotent / missing-series safe.
                    ended_session.clear_metrics()
                if self._primary_game_id == ended_id:
                    self._primary_game_id = None
                    # Mirror _retire_session: the persistent primary bus
                    # must not keep stamping the retired game's id (#776).
                    # A new primary re-stamps it below; this covers the
                    # cap-rejected early return.
                    self.primary_event_bus.current_game_id = ""

            # Decide kind + bus (#837). ONLY a real (menu-origin) game with no
            # live primary claims the primary slot + persistent primary bus.
            # Everything else is a shadow session with its own EventBus, even
            # when idle.
            if is_real and self._primary_game_id is None:
                game_kind = GAME_KIND_PRIMARY
                event_bus = self.primary_event_bus
            else:
                game_kind = GAME_KIND_SHADOW
                event_bus = EventBus()

            # Per-kind concurrency guards (#1018, option b).
            #
            # REAL single-game guard: a real (menu-origin) start with a primary
            # already live was classified SHADOW above (the primary slot is
            # taken). But two REAL games must never coexist, so reject it with
            # the legacy "Game already in progress" message rather than quietly
            # demoting the real start to a shadow. This is the single-game guard
            # the issue scopes to real games — previously emergent from the
            # global cap=1, now explicit.
            if is_real and self._primary_game_id is not None:
                return None, True

            # SHADOW concurrency cap: count live shadow sessions and reject a new
            # shadow once the concurrent-shadow cap is full. This is what bounds
            # concurrent shadows without a real game ever tripping it.
            if game_kind == GAME_KIND_SHADOW:
                live_shadows = sum(1 for sess in self.sessions.values() if sess.game_kind == GAME_KIND_SHADOW)
                if live_shadows >= _max_shadow_games():
                    return None, True

            # Total ceiling (runaway safety net): never register more than the
            # absolute total of primary + shadow sessions.
            if len(self.sessions) >= _max_concurrent_games():
                return None, True

            game_id = f"game_{uuid.uuid4().hex[:12]}"
            # The game-lifecycle span is rooted as its OWN trace (#1157), not as a
            # child of the long-lived StreamGameEvents stream span. We capture the
            # inbound stream/start-RPC span's SpanContext here so the session can
            # add an OTel Link back to it (keeping the inbound call navigable in
            # Jaeger) without inheriting the stream's trace_id as the game's root.
            stream_span_context = parent_span.get_span_context() if parent_span is not None else None

            # Spawn-traceparent propagation (#1182, epic #1181). When the agent
            # spawns a shadow game it injects its long-lived EXPERIMENT span's
            # traceparent into the StreamGameEvents call; the server interceptor
            # extracts it (lib/grpc_tracing.py). We capture the INJECTED context
            # itself (the agent.experiment span), not the coordinator's own SERVER
            # span the interceptor starts under it when grpc_rpc_spans is on, as
            # the experiment target of a shadow + experiment-bound game: its Link
            # (``link``) or its parent (``child``), telemetry.experiment_game_parent
            # (#2223). The strict shadow+experiment gate in GameSession keeps a
            # real game own-rooted (#1157/#1164).
            experiment_parent_span_context = injected_parent_span_context(parent_span)

            # Experiment/cohort spawn binding (#976, epic #982). The caller
            # provides (experiment_id, arm) on StartGameConfig; the registry
            # (#978) will decide+supply them later — #976 is only the plumbing
            # that applies+propagates them. They bind AT SPAWN so the session's
            # eval-context (targeting #977) and telemetry (attribution #975)
            # carry them from the start.
            #
            # UC-043 BR-001 (shadow-only).
            # HARD INVARIANT (#982): experiment_id/arm only SUBDIVIDE a shadow
            # game; they MUST NOT make a real (primary) game part of an
            # experiment. We therefore apply them ONLY when this session is a
            # shadow session, dropping any experiment fields a real-origin start
            # might (erroneously) carry. This keeps the real-protection
            # guarantee structurally intact: a primary GameSession can never be
            # constructed with a non-empty experiment_id/arm.
            #
            # Paired CRN seed (#1003) rides the SAME guard: rng_seed makes a
            # shadow session deterministic for Common-Random-Numbers pairing, but
            # a real game MUST get entropy (0) so its behavior is byte-unchanged.
            # Same invariant — never seed a primary session from a caller value.
            #
            # pattern_trigger_policy (#2096, UC-046 BR-018, UC-040 BR-019) rides it
            # too: it lets a shadow game honour shield.activation trigger/hold
            # because its pattern controllers press the trigger; a real start's
            # value is dropped so it can never change a real game.
            if game_kind == GAME_KIND_SHADOW:
                experiment_id = getattr(config, "experiment_id", "")
                arm = getattr(config, "arm", "")
                rng_seed = getattr(config, "rng_seed", 0)
                pattern_trigger_policy = bool(getattr(config, "pattern_trigger_policy", False))
            else:
                experiment_id = ""
                arm = ""
                rng_seed = 0
                pattern_trigger_policy = False

            session = GameSession(
                game_id=game_id,
                game_name=config.game_name,
                players=list(config.players),
                game_config=config,
                event_bus=event_bus,
                game_kind=game_kind,
                stream_span_context=stream_span_context,
                experiment_parent_span_context=experiment_parent_span_context,
                experiment_id=experiment_id,
                arm=arm,
                rng_seed=rng_seed,
                pattern_trigger_policy=pattern_trigger_policy,
            )
            # The game thread reports the end to the flag service (#1847).
            session.on_game_ended = self._on_session_game_ended
            # Shadow sessions own their bus; bind its state-sync to the
            # session so the bus updates that session's state directly.
            if game_kind == GAME_KIND_SHADOW:
                event_bus._state_sync_callback = session.on_event_state_sync

            # Stamp every event published on this bus with this game's id
            # (#776). The primary bus is persistent, so this mutable field is
            # set on bind and cleared on retire (see _retire_session).
            event_bus.current_game_id = game_id

            self.sessions[game_id] = session
            if game_kind == GAME_KIND_PRIMARY:
                self._primary_game_id = game_id

            return (session, game_kind, game_id), False

    async def _start_game_from_config(
        self,
        config,
        parent_span,
        on_admitted: Callable[[GameSession], Awaitable[None]] | None = None,
    ) -> tuple[bool, str]:
        """
        Start a game from StartGameConfig (internal helper).

        ``on_admitted`` (#1783) runs once the session exists and BEFORE its
        ``game_start`` publish and ``start_thread()``: StreamGameEvents uses it
        to subscribe a headless starter to the new session's bus, so the starter
        receives every event of its game, beginning with ``game_start``.

        Origin governs classification (#837): a ``GAME_ORIGIN_MENU`` start is the
        REAL game and claims the primary slot + persistent primary bus when no
        primary is live; it also preempts (force-ends) any running shadow games
        first. Every other origin (UNSPECIFIED/AGENT) is ALWAYS a shadow session
        with its own EventBus, even when the system is idle. A shadow start is
        rejected by the resource gate while a real game is live and
        ``shadow_policy=block``.

        Returns:
            Tuple of (success, error_message_or_game_id)
        """
        try:
            # Validate player count (cheap, no lock needed): at least two players,
            # and at least the mode's minimum for every origin (UC-010 BR-001,
            # #1862). An unknown mode keeps the two-player floor; the factory
            # rejects it later.
            if len(config.players) < 2:
                return False, "Need at least 2 players"
            mode = Games.from_name(config.game_name)
            if mode is not None and len(config.players) < mode.minimum_players:
                return False, f"{mode.name} needs at least {mode.minimum_players} players"

            is_real = self._is_real_origin(config)

            # Resource gate (#837): a shadow must not run while a real game is
            # live when policy=block. A DISTINCT message (not the legacy
            # "Game already in progress") so agent runners can tell the
            # difference between "capacity full" and "blocked by real game".
            if not is_real and self._has_live_real_game() and _shadow_policy() == "block":
                metrics.shadow_games_blocked_total.inc()
                logger.info("Shadow start rejected: a real game is running and shadow_policy=block")
                return False, "Shadow games blocked while a real game is running"

            # The real game always wins resources: evict running shadows BEFORE
            # the cap check so freed slots count and the real game is never
            # rejected because shadows filled the cap (#837). Preemption runs
            # outside the lock (force_end joins the game thread), so a shadow
            # can slip in between preemption and the lock — the retry preempts
            # again and re-admits, guaranteeing the real game wins that race.
            for attempt in (0, 1):
                if is_real:
                    await self._preempt_shadow_sessions()

                admitted, cap_full = self._admit_session_locked(config, is_real, parent_span)
                if admitted is not None:
                    session, game_kind, game_id = admitted
                    break
                assert cap_full
                if not is_real or attempt == 1:
                    return False, "Game already in progress"
                logger.warning("Real game lost the admission race to a late shadow; preempting again (#837)")
            try:
                if is_real:
                    # A shadow admitted while this real start was preempting (outside
                    # the lock) registered before the primary slot was live, so neither
                    # the preemption pass nor the shadow_policy=block gate saw it.
                    # Preempt it now: no shadow outlives a real start (UC-043 A2,
                    # UC-046 A8, #1404). From here on the live primary blocks new
                    # shadows under policy block. Inside the try: a cancel or an error
                    # in this pass retires the admitted session (UC-010 BR-007).
                    await self._preempt_shadow_sessions()
                return await self._run_admitted_start(session, game_kind, game_id, on_admitted)
            except asyncio.CancelledError:
                # UC-010 BR-007 (#1791): a client cancel after admission retires
                # the session like a normal end; the cancel propagates.
                self._abort_admitted_session(session, "cancelled")
                raise
            except BaseException:
                # UC-010 BR-007 (#1791): an error after admission retires the
                # session like a normal end, so it holds no primary slot,
                # controllers or shadow-cap slot; it becomes the (False, error)
                # answer below.
                self._abort_admitted_session(session, "error")
                raise

        except Exception as e:
            logger.error(f"StartGame error: {e}", exc_info=True)
            return False, str(e)

    def _abort_admitted_session(self, session: GameSession, reason: str) -> None:
        """Retire a session whose start failed or was cancelled after admission.

        ``reason`` is ``"error"`` or ``"cancelled"`` (log only).

        No ``game_ended`` is published: the game never started (UC-010 A7). The
        flag-service ``end`` queued by ``_retire_session`` waits for an in-flight
        registration, so a registration that was made is undone.
        """
        session.game_running = False
        with session._state_lock:
            session.game_state = game_coordinator_pb2.GameState.ENDED
        with self._sessions_lock:
            # A force end or preemption during an await may have retired it already.
            still_registered = self.sessions.get(session.game_id) is session
        if still_registered:
            self._retire_session(session.game_id)
        logger.warning(
            "Start of %s game %s aborted after admission (reason=%s); session retired",
            session.game_kind,
            session.game_id,
            reason,
        )

    async def _run_admitted_start(
        self,
        session: GameSession,
        game_kind: str,
        game_id: str,
        on_admitted: Callable[[GameSession], Awaitable[None]] | None,
    ) -> tuple[bool, str]:
        """Everything after admission: starter hook, flag-service register,
        start gauges, ``game_start`` publish and the game thread."""
        # Register the game with the flag service BEFORE it runs, so agent
        # writes for it are accepted from its first frame (FR-135). Bounded
        # to 200 ms and outside _sessions_lock; on a timeout or an error the
        # game starts anyway and the lifecycle reconciles (C-004).
        if on_admitted is not None:
            await on_admitted(session)

        await self.flag_service.register(self._game_record(session))

        # Update lifecycle metrics for this session's kind (#775) + experiment
        # attribution (#975). experiment_id/arm are "" for a non-experiment
        # game; #976's spawn binding sets them on the session.
        # Written under the session's metrics lock and skipped once retired:
        # a retire during the register await above already exported the 0,
        # and a late 1 would overwrite it (the delayed removal would then
        # delete a series reading 1, so the agent never sees the end).
        def write_start_gauges() -> None:
            labels = {
                "game_kind": game_kind,
                "game_id": game_id,
                "experiment_id": session.experiment_id,
                "arm": session.arm,
            }
            metrics.active_game.labels(**labels).set(1)
            metrics.active_players.labels(**labels).set(len(session.players))

        session._set_gauges_unless_retired(write_start_gauges)

        # Publish game_start on this session's bus.
        await session.event_bus.publish(
            GameEvent.GAME_START,
            {
                "game_name": session.game_name,
                "game_id": game_id,
                "player_count": str(len(session.players)),
            },
        )

        # Start game in background thread (with async support).
        try:
            session.start_thread()
        except Exception as e:
            # UC-010 A7 (#1791): observers of this bus already saw game_start
            # (the dashboard shows Running); game_start_error moves them off it.
            # The caller then retires the session.
            await session.event_bus.publish(
                "game_start_error",
                {"error": str(e), "game_id": game_id},
            )
            raise

        # Counted only once the game thread started (telemetry.md): a start that
        # fails after admission is not a started game. No await since
        # start_thread(), so a cancel can not land between the two.
        # games_started_total is a CUMULATIVE counter: it deliberately does
        # NOT carry experiment_id/arm (a label on a cumulative counter is one
        # permanent series per experiment, forever). Per-experiment counts
        # come from the game span's experiment.id/arm attributes (#975).
        metrics.games_started_total.labels(
            mode=session.game_name,
            game_kind=game_kind,
        ).inc()

        logger.info(f"Started {game_kind} game {game_id}: {session.game_name} with {len(session.players)} players")
        return True, game_id

    # ------------------------------------------------------------------
    # Force-end path
    # ------------------------------------------------------------------

    async def _force_end_current_game(self, reason: str) -> tuple[bool, str]:
        """Force end the PRIMARY game (legacy ``ForceEndGame`` with no game_id).

        No-ops safely (returns ``(False, ...)``) when no primary game is running.

        Returns ``(success, error)``.
        """
        return await self._force_end_session("", reason)

    async def _force_end_session(self, game_id: str, reason: str) -> tuple[bool, str]:
        """Force end exactly the session ``game_id`` (shared by ``ForceEndGame``
        and the ``end_game`` agent intervention).

        ``""`` resolves to the primary session (legacy semantics). A non-empty
        game_id never falls back to the primary: an unknown id is a no-op, so an
        ``end_game`` evaluated for a shadow can only end that shadow (UC-040
        BR-011, #1310).

        Returns ``(success, error)``.
        """
        # UC-027 BR-001: a given id never falls back to the primary; only "" targets the primary game.
        session = self._resolve_session(game_id)
        if session is None:
            return False, (f"Unknown game_id: {game_id}" if game_id else "No game in progress")

        # UC-027 A4 / BR-003: one end per session, shared with a preemption or
        # another force end under way; it completes even if this caller is
        # cancelled during the join (#1827).
        return await asyncio.shield(self._session_ending_task(session, reason))

    @staticmethod
    def _game_record(session: GameSession) -> GameRecord:
        """The flag service's view of a session. The telemetry kind ``primary``
        is the flag vocabulary's ``real`` (FR-041, #1249)."""
        return GameRecord(
            game_id=session.game_id,
            game_kind="real" if session.game_kind == GAME_KIND_PRIMARY else "shadow",
            serials=tuple(player.serial for player in session.players),
            experiment_id=session.experiment_id or "",
            arm=session.arm or "",
        )

    def _flag_service_live_games(self) -> list[GameRecord]:
        """Every registered, not yet ended session (the reconcile set, UC-053 A10)."""
        with self._sessions_lock:
            sessions = list(self.sessions.values())
        return [self._game_record(s) for s in sessions if s.game_state != game_coordinator_pb2.GameState.ENDED]

    def _queue_flag_service_end_locked(self, game_id: str, session: GameSession | None) -> None:
        """Queue the flag-service end of ``game_id`` once per session (#1847).

        Caller holds ``_sessions_lock``. The game thread's end, the ENDED sweep,
        a force end and a preemption can each reach a session's end; only the
        first queues it. Without a session (already popped) the end is queued
        unconditionally, as before #1847.
        """
        if session is not None:
            if session.flag_service_end_queued:
                return
            session.flag_service_end_queued = True
        self.flag_service.end(game_id)

    def _on_session_game_ended(self, session: GameSession) -> None:
        """Game-thread hook: the session's game ended (UC-053 step 7, #1847).

        Queues only the flag-service end (thread-safe, no network call here).
        The session stays registered, so GetGameState keeps answering ENDED until
        the next start's sweep retires it; a force end or preemption retires it
        right away. Runs on the game thread, which never holds ``_sessions_lock``.
        """
        with self._sessions_lock:
            self._queue_flag_service_end_locked(session.game_id, session)

    def _retire_session(self, game_id: str) -> None:
        """Remove a finished session from the registry and free the primary slot
        if it owned it, so a new primary can start."""
        with self._sessions_lock:
            session = self.sessions.pop(game_id, None)
            self.intervention_manager.forget_session(game_id)
            # Queued, not sent: no network call under the lock (FR-135).
            self._queue_flag_service_end_locked(game_id, session)
            if session is not None:
                # Remove this session's game_id-labeled gauge series so the
                # zeroed-but-persistent series don't accumulate as shadows churn
                # short-lived game_ids (#1018). Idempotent / missing-series safe.
                session.clear_metrics()
            if self._primary_game_id == game_id:
                self._primary_game_id = None
                # The primary bus is persistent and outlives the session; clear
                # its game_id stamp so post-retire idle events carry empty
                # game_id (#776). Shadow buses are discarded, no cleanup needed.
                self.primary_event_bus.current_game_id = ""

    async def ForceEndGame(self, request, _context):
        """Force end a game (#776).

        ``request.game_id`` selects the target session; empty resolves to the
        primary session (exact legacy semantics). An unknown game_id returns
        success=False with a clear error.
        """
        try:
            game_id = request.game_id or ""

            if not game_id:
                # Legacy path: end the primary game.
                success, error = await self._force_end_current_game(request.reason)
                return game_coordinator_pb2.ForceEndGameResponse(success=success, error=error)

            success, error = await self._force_end_session(game_id, request.reason)
            return game_coordinator_pb2.ForceEndGameResponse(success=success, error=error)
        except Exception as e:
            logger.error(f"ForceEndGame error: {e}", exc_info=True)
            return game_coordinator_pb2.ForceEndGameResponse(success=False, error=str(e))

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def StreamGameEvents(self, request, context):
        """
        Stream game events in real-time.

        Subscription target (#776):
        - start_config provided: start a new game and subscribe to THAT session's
          bus (so a headless starter receives its own game's events). Any
          request.game_id is ignored in this case.
        - start_config empty, request.game_id set: subscribe to that game's bus;
          an unknown game_id yields a ``game_error`` event and closes the stream.
        - start_config empty, game_id empty: subscribe to the persistent primary
          bus (legacy zero-arg behavior — the menu's subscribe-before-start path).
        """
        subscriber_id = f"events_{time.time()}"

        # Enrich the server span created by the gRPC interceptor
        span = trace.get_current_span()
        span.set_attribute("subscriber.id", subscriber_id)

        # Default subscription target is the persistent primary bus.
        event_bus = self.primary_event_bus
        event_queue: asyncio.Queue | None = None

        # Check if this is a game start request
        if request.HasField("start_config"):
            config = request.start_config
            span.set_attribute("game.name", config.game_name)
            span.set_attribute("player.count", len(config.players))
            span.set_attribute("game.start_via_stream", True)

            # Subscribe to the new session's bus BEFORE game_start is published
            # and the game thread starts (#1783): the starter receives every
            # event of its own game, beginning with game_start (which carries
            # the game_id). Subscribing after start_thread() lost the events the
            # thread published first (EventBus has no replay).
            subscribed_bus: list[EventBus] = []

            async def _subscribe_starter(session: GameSession) -> None:
                nonlocal event_queue
                event_queue = await session.event_bus.subscribe(subscriber_id)
                subscribed_bus.append(session.event_bus)

            async def _unsubscribe_starter() -> None:
                for bus in subscribed_bus:
                    await bus.unsubscribe(subscriber_id)

            try:
                success, result = await self._start_game_from_config(config, span, on_admitted=_subscribe_starter)
            except BaseException:
                # A client cancel mid-start (CancelledError, e.g. during the
                # flag-service register) skips the paths below: drop the
                # starter's subscriber here so it does not leak (#1783).
                await _unsubscribe_starter()
                raise

            if not success:
                # No leaked subscriber on a failed start (#1783).
                await _unsubscribe_starter()
                # Yield error event and close stream
                logger.error(f"Failed to start game via stream: {result}")
                span.set_attribute("error", result)
                yield game_coordinator_pb2.GameEvent(
                    event_type="game_start_error",
                    data={"error": result},
                    timestamp=int(time.time() * 1000),
                )
                return

            span.set_attribute("game.id", result)
            logger.info(f"Game {result} started via stream")

            if subscribed_bus:
                event_bus = subscribed_bus[0]

        elif request.game_id:
            # Subscribe-by-game_id (#776): route to that session's bus.
            span.set_attribute("game.id", request.game_id)
            session = self._resolve_session(request.game_id)
            if session is None:
                logger.warning(f"StreamGameEvents: unknown game_id {request.game_id}")
                span.set_attribute("error", "unknown_game_id")
                yield game_coordinator_pb2.GameEvent(
                    event_type="game_error",
                    data={"error": f"Unknown game_id: {request.game_id}"},
                    timestamp=int(time.time() * 1000),
                    game_id=request.game_id,
                )
                return
            event_bus = session.event_bus

        # Subscribe to event bus (a start_config stream already subscribed above).
        if event_queue is None:
            event_queue = await event_bus.subscribe(subscriber_id)

        try:
            # Headers now = "subscriber registered" (#1613); inside the try so a client that leaves
            # during the send still runs the unsubscribe below.
            await context.send_initial_metadata(())
            while not context.cancelled():
                try:
                    # Async wait with timeout
                    event = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                    yield event

                except TimeoutError:
                    # No event, continue (timeout keeps connection alive)
                    continue
                except Exception as e:
                    logger.error(f"Stream error for {subscriber_id}: {e}")
                    break

        finally:
            # Cleanup via EventBus
            await event_bus.unsubscribe(subscriber_id)

    # ------------------------------------------------------------------
    # State query
    # ------------------------------------------------------------------

    @staticmethod
    def _build_game_info(session: "GameSession") -> game_coordinator_pb2.GameInfo:
        """Snapshot a session into a GameInfo proto (#776).

        Shared by GetGameState and ListGames. Acquires the session's state lock
        so the game_state/players snapshot is consistent.
        """
        with session._state_lock:
            game_info = game_coordinator_pb2.GameInfo(
                game_mode=session.game_name or "",
                state=session.game_state,
                game_id=session.game_id or "",
                start_time_ms=int((session.game_start_time or 0) * 1000),
            )

            current_game = session.current_game
            if current_game and hasattr(current_game, "players"):
                teams = getattr(current_game, "teams", {})

                for serial, player in current_game.players.items():
                    team_name = ""
                    if player.team >= 0 and player.team in teams:
                        team_name = teams[player.team].name

                    color = player.color if player.color else (0, 0, 0)
                    r, g, b = color[0], color[1], color[2]

                    player_info = game_coordinator_pb2.PlayerInfo(
                        serial=serial,
                        team=player.team,
                        team_name=team_name,
                        color=game_coordinator_pb2.RGB(r=r, g=g, b=b),
                        alive=player.alive,
                        sensitivity_factor=player.sensitivity_factor,
                        score=0,  # Score tracking not yet implemented in base Player
                    )
                    game_info.players.append(player_info)

        return game_info

    async def GetGameState(self, request, _context):
        """
        Get a game's state for testing and observability.

        ``request.game_id`` selects the target session; empty resolves to the
        primary session (exact legacy semantics). An unknown game_id returns
        success=False. Returns detailed player information including team
        assignments, colors, and alive status.
        """
        try:
            game_id = request.game_id or ""

            # Unknown explicit game_id is an error (legacy empty-id idle stays a
            # success with an IDLE GameInfo).
            if game_id and self._resolve_session(game_id) is None:
                return game_coordinator_pb2.GetGameStateResponse(
                    success=False,
                    error=f"Unknown game_id: {game_id}",
                )

            session = self._resolve_session(game_id)

            if session is None:
                game_info = game_coordinator_pb2.GameInfo(
                    game_mode="",
                    state=game_coordinator_pb2.GameState.IDLE,
                    game_id="",
                    start_time_ms=0,
                )
                return game_coordinator_pb2.GetGameStateResponse(success=True, error="", game_info=game_info)

            game_info = self._build_game_info(session)

            logger.debug(
                f"GetGameState: mode={game_info.game_mode}, state={game_info.state}, players={len(game_info.players)}"
            )
            return game_coordinator_pb2.GetGameStateResponse(
                success=True,
                error="",
                game_info=game_info,
            )

        except Exception as e:
            logger.error(f"GetGameState error: {e}", exc_info=True)
            return game_coordinator_pb2.GetGameStateResponse(
                success=False,
                error=str(e),
            )

    async def ListGames(self, _request, _context):
        """List all live game sessions (primary + shadow) (#776).

        Returns a GameInfo per registered session so agents and tests can
        enumerate concurrent games and learn their game_ids.
        """
        try:
            with self._sessions_lock:
                sessions = list(self.sessions.values())

            response = game_coordinator_pb2.ListGamesResponse(success=True, error="")
            for session in sessions:
                response.games.append(self._build_game_info(session))
            logger.debug(f"ListGames: {len(response.games)} live game(s)")
            return response
        except Exception as e:
            logger.error(f"ListGames error: {e}", exc_info=True)
            return game_coordinator_pb2.ListGamesResponse(success=False, error=str(e))

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self):
        """Shutdown the game coordinator: join all session threads, then flush and
        stop the flag-service lifecycle (so ends queued while joining are sent)."""
        logger.info("Shutting down GameCoordinator...")

        # Let session ends whose caller was cancelled finish first (#1816,
        # #1827), so they retire their session before the flush below.
        pending_endings = [task for _session, task in self._session_endings.values()]
        if pending_endings:
            await asyncio.wait(pending_endings, timeout=3)

        # Stop intervention flag subscription (#730)
        try:
            self.intervention_manager.stop()
        except Exception as e:
            logger.debug(f"InterventionManager stop failed: {e}")

        # Stop tempo-schedule SOURCE subscription (#1109)
        try:
            self.tempo_schedule_manager.stop()
        except Exception as e:
            logger.debug(f"TempoScheduleManager stop failed: {e}")

        with self._sessions_lock:
            sessions = list(self.sessions.values())

        logger.info(f"Joining {len(sessions)} game session thread(s)...")
        for session in sessions:
            try:
                await session.join_thread()
            except Exception as e:
                logger.debug(f"Session {session.game_id} join failed: {e}")

        # Last: the joined sessions' end hooks have queued their flag-service
        # ends; give them a bounded chance to be sent, then stop (#1849).
        try:
            await self.flag_service.flush()
        except Exception as e:
            logger.debug(f"Flag service lifecycle flush failed: {e}")
        try:
            await self.flag_service.stop()
        except Exception as e:
            logger.debug(f"Flag service lifecycle stop failed: {e}")
