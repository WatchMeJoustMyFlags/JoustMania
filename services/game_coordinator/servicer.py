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
"subscribe before any game exists" semantics: the first session started while no
primary is active becomes the primary and publishes to this bus; concurrent
secondary (shadow) sessions each get their own EventBus. Zero-arg
``StreamGameEvents`` subscribers attach to the primary bus exactly as before; a
``StreamGameEvents`` call WITH start_config subscribes to the bus of the session
it just created. ``ForceEndGame`` / ``GetGameState`` (no game_id in the proto
yet — that is #776) operate on the primary session.
"""

import asyncio
import logging
import os
import threading
import time
import uuid

from opentelemetry import trace

from lib.telemetry import get_tracer
from lib.types import GameEvent
from proto import game_coordinator_pb2, game_coordinator_pb2_grpc
from services.game_coordinator import metrics
from services.game_coordinator.difficulty_handlers import register_difficulty_handlers
from services.game_coordinator.event_bus import EventBus
from services.game_coordinator.game_session import GAME_KIND_PRIMARY, GAME_KIND_SHADOW, GameSession
from services.game_coordinator.interventions import InterventionManager
from services.game_coordinator.lifecycle_handlers import register_lifecycle_handlers

logger = logging.getLogger(__name__)

# Lazy telemetry initialization - defers OTLP setup until first span
tracer = get_tracer(__name__)

# Concurrency cap (#775). Default 1 preserves today's behavior exactly: the
# second concurrent start is rejected with the legacy "Game already in progress"
# message. Tests/agents raise it for multi-session.
DEFAULT_MAX_CONCURRENT_GAMES = 1


def _max_concurrent_games() -> int:
    """Read the concurrent-games cap from the environment (default 1)."""
    raw = os.getenv("GAME_MAX_CONCURRENT_GAMES", str(DEFAULT_MAX_CONCURRENT_GAMES))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_CONCURRENT_GAMES
    return max(1, value)


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

        # Persistent PRIMARY EventBus — created once, NEVER destroyed. Zero-arg
        # StreamGameEvents subscribers (the menu) attach here even before any
        # game exists, preserving legacy subscribe-before-start semantics. Its
        # state-sync callback routes to whichever session owns the primary slot.
        self.primary_event_bus = EventBus(state_sync_callback=self._on_primary_event_state_sync)

        # Random game history
        self.random_history: list[str] = []

        # Agent intervention manager (#730): subscribes to the flagd
        # `interventions` domain and applies agent interventions to the live
        # (primary) game via the enforcement chain. get_live_game() returns the
        # primary session's running BaseGameMode so handlers act on it.
        self.intervention_manager = InterventionManager(
            event_publisher=self.primary_event_bus.publish,
            get_game=self.get_live_game,
            end_game_fn=self._force_end_current_game,
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

    def get_live_game(self):
        """Return the currently running PRIMARY game instance, or None.

        Used by the InterventionManager so intervention handlers act on the
        live (menu-driven) game.
        """
        return self.current_game

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

    async def _start_game_from_config(self, config, parent_span) -> tuple[bool, str]:
        """
        Start a game from StartGameConfig (internal helper).

        Creates a new GameSession, registers it, and launches its background
        thread. The first session started while no primary is active becomes the
        primary (and uses the persistent primary bus); additional concurrent
        sessions are shadow sessions, each with its own EventBus.

        Returns:
            Tuple of (success, error_message_or_game_id)
        """
        try:
            # Validate player count (cheap, no lock needed).
            if len(config.players) < 2:
                return False, "Need at least 2 players"

            with self._sessions_lock:
                # Concurrency gate (#775). Default cap 1 reproduces the legacy
                # single-game rejection message exactly.
                if len(self.sessions) >= _max_concurrent_games():
                    return False, "Game already in progress"

                # Decide kind + bus. First session with no live primary becomes
                # the primary and reuses the persistent primary bus.
                if self._primary_game_id is None:
                    game_kind = GAME_KIND_PRIMARY
                    event_bus = self.primary_event_bus
                else:
                    game_kind = GAME_KIND_SHADOW
                    event_bus = EventBus()

                game_id = f"game_{uuid.uuid4().hex[:12]}"
                parent_context = trace.set_span_in_context(parent_span)

                session = GameSession(
                    game_id=game_id,
                    game_name=config.game_name,
                    players=list(config.players),
                    game_config=config,
                    event_bus=event_bus,
                    game_kind=game_kind,
                    parent_context=parent_context,
                )
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

            # Update lifecycle metrics for this session's kind.
            metrics.active_game.labels(game_kind=game_kind).set(1)
            metrics.games_started_total.labels(mode=session.game_name, game_kind=game_kind).inc()
            metrics.active_players.labels(game_kind=game_kind).set(len(session.players))

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
            session.start_thread()

            logger.info(f"Started {game_kind} game {game_id}: {session.game_name} with {len(session.players)} players")
            return True, game_id

        except Exception as e:
            logger.error(f"StartGame error: {e}", exc_info=True)
            return False, str(e)

    # ------------------------------------------------------------------
    # Force-end path
    # ------------------------------------------------------------------

    async def _force_end_current_game(self, reason: str) -> tuple[bool, str]:
        """Force end the PRIMARY game (shared by ForceEndGame RPC and the
        ``end_game`` agent intervention, #730).

        Until game_id routing lands (#776), this targets the primary session.
        No-ops safely (returns ``(False, ...)``) when no primary game is running.

        Returns ``(success, error)``.
        """
        session = self._get_primary_session()
        if session is None:
            return False, "No game in progress"

        success, error = await session.force_end(reason)
        if success:
            self._retire_session(session.game_id)
        return success, error

    def _retire_session(self, game_id: str) -> None:
        """Remove a finished session from the registry and free the primary slot
        if it owned it, so a new primary can start."""
        with self._sessions_lock:
            self.sessions.pop(game_id, None)
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

            session = self._resolve_session(game_id)
            if session is None:
                return game_coordinator_pb2.ForceEndGameResponse(success=False, error=f"Unknown game_id: {game_id}")

            success, error = await session.force_end(request.reason)
            if success:
                self._retire_session(session.game_id)
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

        # Check if this is a game start request
        if request.HasField("start_config"):
            config = request.start_config
            span.set_attribute("game.name", config.game_name)
            span.set_attribute("player.count", len(config.players))
            span.set_attribute("game.start_via_stream", True)

            # Start the game
            success, result = await self._start_game_from_config(config, span)

            if not success:
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

            # Subscribe to the bus of the session we just created so a headless
            # starter receives its own game's events.
            started = self.sessions.get(result)
            if started is not None:
                event_bus = started.event_bus

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

        # Subscribe to event bus
        event_queue = await event_bus.subscribe(subscriber_id)

        try:
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
        """Shutdown the game coordinator: join all session threads."""
        logger.info("Shutting down GameCoordinator...")

        # Stop intervention flag subscription (#730)
        try:
            self.intervention_manager.stop()
        except Exception as e:
            logger.debug(f"InterventionManager stop failed: {e}")

        with self._sessions_lock:
            sessions = list(self.sessions.values())

        logger.info(f"Joining {len(sessions)} game session thread(s)...")
        for session in sessions:
            try:
                await session.join_thread()
            except Exception as e:
                logger.debug(f"Session {session.game_id} join failed: {e}")
