"""Menu gRPC Servicer for JoustMania."""

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from lib.feature_flags import (
    GAME_KIND_REAL,
    get_flag_client,
    init_flag_domain,
    set_game_transaction_context,
)
from lib.flag_config_writer import FlagConfigWriter
from lib.flagd_paths import active_flag_file
from lib.telemetry import get_tracer
from lib.types import Games
from proto import menu_pb2, menu_pb2_grpc
from services.menu import metrics
from services.menu.controller_events import ControllerEventLoop
from services.menu.event_publisher import EventPublisher
from services.menu.handlers import AdminModeHandler, ConnectedHandler, IdleHandler, ReadyHandler
from services.menu.handlers.base import ControllerState
from services.menu.idle_monitor import IdleMonitor, read_sentinel_animation
from services.menu.state_manager import StateManager
from services.menu.utils import AudioHelper, LedController
from services.menu.utils.audio import GAME_MODE_VOICE
from services.menu.utils.settings import DEFAULT_GAME_MODE, DEFAULT_VOICE_ACTOR, GAME_MODES, get_next_game_mode

logger = logging.getLogger(__name__)

# Lazy telemetry initialization - defers OTLP setup until first span
tracer = get_tracer(__name__)


@dataclass
class _GameEventResult:
    """Result from processing game events in a stream iteration."""

    game_started: bool
    should_return: bool


class MenuServicer(menu_pb2_grpc.MenuServiceServicer):
    """
    Menu gRPC servicer.

    Manages menu UI and interactions:
    - Start/stop menu
    - Process input
    - Stream events
    """

    def __init__(self):
        """Initialize menu service."""
        self.state = menu_pb2.MenuState.STOPPED
        self.current_selection: Games = Games.JoustFFA

        # Persistent gRPC channels
        from lib.grpc_utils import create_channel

        controller_host = os.getenv("CONTROLLER_MANAGER_HOST", "controller-manager")
        controller_port = os.getenv("CONTROLLER_MANAGER_PORT", "50052")
        game_coordinator_host = os.getenv("GAME_COORDINATOR_HOST", "game-coordinator")
        game_coordinator_port = os.getenv("GAME_COORDINATOR_PORT", "50053")
        audio_host = os.getenv("AUDIO_HOST", "audio")
        audio_port = os.getenv("AUDIO_PORT", "50056")

        self.controller_channel = create_channel(f"{controller_host}:{controller_port}")
        self.game_coordinator_channel = create_channel(f"{game_coordinator_host}:{game_coordinator_port}")
        self.audio_channel = create_channel(f"{audio_host}:{audio_port}")

        # Initialize flagd domains for game, user, and system settings
        init_flag_domain("game")
        init_flag_domain("user")
        init_flag_domain("system")

        # FlagConfigWriter instances for persisting settings changes. Resolve the
        # active flag directory through the single source of truth (#959/#966):
        # $FLAGD_FLAG_DIR/<domain>.json — the same plain join flagd, the agent
        # writers, and the integration tests use, so a menu-driven flag write
        # lands in the file flagd actually serves.
        self.game_settings_writer = FlagConfigWriter(str(active_flag_file("game")))
        self.user_prefs_writer = FlagConfigWriter(str(active_flag_file("user")))

        # OpenFeature clients for reading settings
        self.game_settings_client = get_flag_client("game")
        self.user_prefs_client = get_flag_client("user")
        self.system_client = get_flag_client("system")

        # Utility classes
        self.led = LedController(self.controller_channel)
        self.audio = AudioHelper(self.audio_channel, user_prefs_client=self.user_prefs_client)

        # Event publisher for streaming menu events
        self.event_publisher = EventPublisher(tracer, metrics)

        # State manager and handlers
        self.state_manager = StateManager(
            led=self.led,
            audio=self.audio,
            game_settings_writer=self.game_settings_writer,
            user_prefs_writer=self.user_prefs_writer,
            publish_event=self.event_publisher.publish,
        )

        # Register handlers with state manager
        self.connected_handler = ConnectedHandler()
        self.ready_handler = ReadyHandler(start_game_callback=self._start_game)
        self.state_manager.register_handler(self.connected_handler)
        self.state_manager.register_handler(self.ready_handler)

        # Voice actor setting (aaron or ivy)
        self.voice_actor = "ivy"

        # Admin mode handler
        self.admin_handler = AdminModeHandler(
            controller_channel=self.controller_channel,
            tracer=tracer,
            callbacks=self,
            metrics=metrics,
        )
        self.state_manager.register_handler(self.admin_handler)

        # Idle handler and monitor
        self.idle_handler = IdleHandler()
        self.state_manager.register_handler(self.idle_handler)

        self.idle_monitor = IdleMonitor(
            state_manager=self.state_manager,
            get_idle_enabled=lambda: self.system_client.get_boolean_value("idle.enabled", True),
            get_idle_timeout=lambda: self.system_client.get_integer_value("idle.timeout_minutes", 15),
            get_sentinel_count=lambda: self.system_client.get_integer_value("sentinel.count", 2),
            get_rotation_minutes=lambda: self.system_client.get_integer_value("sentinel.rotation_minutes", 5),
            get_sentinel_animation=lambda: read_sentinel_animation(self.system_client),
        )

        # Wire idle handler's wake callback to idle monitor
        self.idle_handler.set_wake_callback(self.idle_monitor.wake_all)

        # Controller event loop (handles button and connection events)
        self.controller_events = ControllerEventLoop(
            controller_channel=self.controller_channel,
            led=self.led,
            callbacks=self,
            metrics=metrics,
        )

        # Track game lifecycle task for clean shutdown
        self._game_lifecycle_task: asyncio.Task | None = None

        logger.info("Menu service initialized")

    # AdminModeCallbacks protocol implementation

    def set_menu_state(self, state) -> None:
        """Set the menu state (AdminModeCallbacks)."""
        self.state = state

    def get_game_options(self) -> list[str]:
        """Get list of available game options (AdminModeCallbacks)."""
        return self.game_options if hasattr(self, "game_options") else []

    # ControllerEventCallbacks protocol implementation

    def get_menu_state(self) -> int:
        """Get current menu state (ControllerEventCallbacks)."""
        return self.state

    async def on_connect(self, serial: str) -> None:
        """Handle controller connect event (ControllerEventCallbacks)."""
        await self.state_manager.on_controller_connected(serial)

    async def on_disconnect(self, serial: str) -> None:
        """Handle controller disconnect event (ControllerEventCallbacks)."""
        await self.state_manager.on_controller_disconnected(serial)

    def update_battery(self, serial: str, battery: int) -> None:
        """Update battery level for a controller (ControllerEventCallbacks)."""
        self.state_manager.update_battery(serial, battery)

    def get_connected_serials(self) -> set[str]:
        """Get set of serials the menu considers connected (ControllerEventCallbacks)."""
        return self.state_manager.connected_controllers

    async def on_stream_reconnected(self) -> None:
        """Clear state on stream reconnect so initial events re-populate cleanly (ControllerEventCallbacks)."""
        await self.state_manager.on_stream_reconnected()

    async def on_button(self, serial: str, button: str, is_press: bool) -> None:
        """Handle button event (ControllerEventCallbacks)."""
        # Reset idle timer on any button activity
        self.idle_monitor.reset_activity()

        # Check for admin mode combo (all 4 face buttons pressed)
        button_state = self.state_manager.button_states.get(serial, {})
        if is_press and button in ["cross", "circle", "square", "triangle"]:
            preview_state = dict(button_state)
            preview_state[button] = True
            if self.admin_handler.check_combo_from_state(preview_state):
                if not self.admin_handler.combo_shown:
                    self.admin_handler.combo_shown = True
                    from services.menu.handlers.base import ControllerState

                    await self.state_manager.transition_to(serial, ControllerState.ADMIN)
                await self.state_manager.handle_button_event(serial, button, is_press)
                return

        # Reset admin combo flag when any face button released
        if not is_press and button in ["cross", "circle", "square", "triangle"]:
            self.admin_handler.combo_shown = False

        await self.state_manager.handle_button_event(serial, button, is_press)

    # Controller state properties (delegate to state_manager as single source of truth)

    @property
    def ready_controllers(self) -> set[str]:
        """Controllers in READY state (delegates to state_manager)."""
        return self.state_manager.ready_controllers

    @property
    def connected_controllers(self) -> set[str]:
        """All connected controllers (delegates to state_manager)."""
        return self.state_manager.connected_controllers

    @property
    def ready_controller_count(self) -> int:
        """Number of ready controllers (computed from state_manager)."""
        return len(self.state_manager.ready_controllers)

    def _clear_ready_state(self) -> None:
        """Clear ready controllers and associated metrics.

        Transitions all READY controllers back to CONNECTED state and
        clears associated metrics. Used when game starts or menu stops.
        """
        # Transition ready controllers back to connected
        for serial in self.state_manager.ready_controllers:
            self.state_manager.controller_states[serial] = ControllerState.CONNECTED

        # Clear metrics
        metrics.player_ready.clear()
        logger.debug("Ready state cleared")

    # gRPC service methods (names defined by proto)

    async def StartMenu(self, _request, _context):
        """Start the menu."""
        start_time = time.time()
        span = trace.get_current_span()
        try:
            if self.state == menu_pb2.MenuState.RUNNING:
                metrics.grpc_requests_total.labels(method="StartMenu", status="already_running").inc()
                return menu_pb2.StartMenuResponse(success=False, error="Menu already running")

            self.state = menu_pb2.MenuState.RUNNING
            self._clear_ready_state()

            # Load settings from flagd (user domain)
            self.voice_actor = self.user_prefs_client.get_string_value("menu_voice", DEFAULT_VOICE_ACTOR)
            current_game_name = self.user_prefs_client.get_string_value("current_game", DEFAULT_GAME_MODE.name)
            game = Games.from_name(current_game_name)
            self.current_selection = game if game and game.name in GAME_MODES else DEFAULT_GAME_MODE
            self.state_manager.set_game_mode(self.current_selection)

            await self.audio.start_lobby_music()
            self.idle_monitor.start()
            await self.event_publisher.publish("menu_started", {})

            logger.info("Menu started")
            span.set_attribute("menu.state", "RUNNING")
            metrics.grpc_requests_total.labels(method="StartMenu", status="ok").inc()

            return menu_pb2.StartMenuResponse(success=True, error="")

        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            logger.error(f"StartMenu error: {e}", exc_info=True)
            metrics.grpc_requests_total.labels(method="StartMenu", status="error").inc()
            return menu_pb2.StartMenuResponse(success=False, error=str(e))
        finally:
            metrics.grpc_request_duration_seconds.labels(method="StartMenu").observe(time.time() - start_time)

    async def StopMenu(self, _request, _context):
        """Stop the menu."""
        start_time = time.time()
        span = trace.get_current_span()
        try:
            if self.state == menu_pb2.MenuState.STOPPED:
                metrics.grpc_requests_total.labels(method="StopMenu", status="already_stopped").inc()
                return menu_pb2.StopMenuResponse(success=False, error="Menu already stopped")

            self.state = menu_pb2.MenuState.STOPPED

            # Stop idle monitor
            self.idle_monitor.stop()

            # Clear all lobby state
            self._clear_ready_state()
            self.state_manager.controller_states.clear()

            await self.event_publisher.publish("menu_stopped", {})

            logger.info("Menu stopped")
            span.set_attribute("menu.state", "STOPPED")
            metrics.grpc_requests_total.labels(method="StopMenu", status="ok").inc()

            return menu_pb2.StopMenuResponse(success=True, error="")

        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            logger.error(f"StopMenu error: {e}", exc_info=True)
            metrics.grpc_requests_total.labels(method="StopMenu", status="error").inc()
            return menu_pb2.StopMenuResponse(success=False, error=str(e))
        finally:
            metrics.grpc_request_duration_seconds.labels(method="StopMenu").observe(time.time() - start_time)

    async def ProcessInput(self, request, _context):
        """Process menu input."""
        start_time = time.time()
        span = trace.get_current_span()
        span.set_attribute("input.type", request.input_type)

        try:
            input_type = request.input_type
            data = dict(request.data)

            if input_type == "button_press":
                await self._handle_button_input(data, span)
            elif input_type == "web_command":
                await self._handle_web_command(data, span)
            elif input_type == "reset_menu":
                await self._handle_reset_menu()

            metrics.grpc_requests_total.labels(method="ProcessInput", status="ok").inc()
            return menu_pb2.ProcessInputResponse(success=True, error="")

        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            logger.error(f"ProcessInput error: {e}", exc_info=True)
            metrics.grpc_requests_total.labels(method="ProcessInput", status="error").inc()
            return menu_pb2.ProcessInputResponse(success=False, error=str(e))
        finally:
            metrics.grpc_request_duration_seconds.labels(method="ProcessInput").observe(time.time() - start_time)

    async def StreamMenuEvents(self, _request, context):
        """Stream menu events in real-time."""
        subscriber_id = f"menu_events_{time.time()}"
        metrics.stream_connections_active.inc()

        span = trace.get_current_span()
        span.set_attribute("subscriber.id", subscriber_id)

        event_queue = await self.event_publisher.subscribe(subscriber_id)

        try:
            while not context.cancelled():
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                    yield event
                except TimeoutError:
                    continue
                except Exception as e:
                    logger.error(f"Stream error for {subscriber_id}: {e}")
                    break
        finally:
            await self.event_publisher.unsubscribe(subscriber_id)
            metrics.stream_connections_active.dec()
            metrics.stream_disconnections_total.inc()

    # Input handlers

    async def _handle_button_input(self, data: dict, span) -> None:
        """Handle button_press input type."""
        button = data.get("button", "")
        span.set_attribute("button", button)

        if button == "trigger":
            await self._start_game(serial="process_input", source="button_input")

        elif button == "select":
            self.current_selection = get_next_game_mode(self.current_selection)
            self.state_manager.set_game_mode(self.current_selection)
            await self.event_publisher.publish("selection_changed", {"game_name": self.current_selection.name})
            # Persist to flagd
            self.user_prefs_writer.update_default_variant("current_game", self.current_selection.name)
            logger.info(f"Selection changed to: {self.current_selection.name}")

    async def _handle_web_command(self, data: dict, span) -> None:
        """Handle web_command input type."""
        command = data.get("command", "")
        span.set_attribute("command", command)

        if command == "start_game":
            await self._start_game(serial="web", source="web")

        elif command == "select_game":
            game_name = data.get("game_name", "")
            game_mode = Games.from_name(game_name)
            if game_mode is not None and game_name in GAME_MODES:
                self.current_selection = game_mode
                self.state_manager.set_game_mode(game_mode)
                await self.event_publisher.publish("selection_changed", {"game_name": game_mode.name, "source": "web"})
                # Persist to flagd
                self.user_prefs_writer.update_default_variant("current_game", game_mode.name)
                voice_file = GAME_MODE_VOICE.get(game_mode.name)
                if voice_file:
                    await self.audio.play_voice(voice_file)
                logger.info(f"Game selected via web: {game_mode.name}")
            else:
                logger.warning(f"Unknown game mode requested via web: {game_name}")

    async def _handle_reset_menu(self) -> None:
        """Handle reset_menu input type."""
        if self.state == menu_pb2.MenuState.GAME_STARTING:
            self.state = menu_pb2.MenuState.RUNNING
            await self.event_publisher.publish("game_start_cancelled", {})
            logger.info("Menu reset to RUNNING state (game start cancelled)")

    # Lifecycle methods

    async def start_button_monitor(self):
        """Start the controller event loop."""
        await self.controller_events.start()

    async def stop_button_monitor(self):
        """Stop the controller event loop."""
        await self.controller_events.stop()

    @property
    def button_monitor_running(self) -> bool:
        """Check if button monitor is running."""
        return self.controller_events.is_running

    async def start_game_event_monitor(self):
        """Start the game event monitoring task (no-op, kept for compatibility)."""
        # Game events are now handled by _start_game() stream directly
        # No separate persistent subscription needed
        logger.info("Game event monitor: events handled by game stream")

    async def stop_game_event_monitor(self):
        """Stop the game lifecycle task if running."""
        if self._game_lifecycle_task is not None and not self._game_lifecycle_task.done():
            logger.info("Cancelling game lifecycle task...")
            self._game_lifecycle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._game_lifecycle_task
            self._game_lifecycle_task = None
            logger.info("Game lifecycle task cancelled")
        else:
            logger.info("No active game lifecycle task to stop")

    async def shutdown(self):
        """Cleanup resources on shutdown."""
        logger.info("Shutting down Menu service...")

        # Stop idle monitor
        self.idle_monitor.stop()

        # Cancel game lifecycle task before closing channels
        if self._game_lifecycle_task is not None and not self._game_lifecycle_task.done():
            logger.info("Cancelling game lifecycle task during shutdown...")
            self._game_lifecycle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._game_lifecycle_task
            self._game_lifecycle_task = None

        logger.info("Closing gRPC channels...")
        await self.controller_channel.close()
        await self.game_coordinator_channel.close()
        await self.audio_channel.close()
        logger.info("Menu service gRPC channels closed")

    async def _start_game(self, serial: str, source: str = "controller"):
        """
        Schedule game start as a background task.

        This is called from button event handlers, so we can't block or stop
        the button monitor synchronously. Instead, schedule the game lifecycle
        to run after a brief delay.

        Args:
            serial: Controller serial that triggered the start (for logging)
            source: Source of the start request ("controller" or "web")
        """
        # Cancel any existing game lifecycle task (defensive)
        if self._game_lifecycle_task is not None and not self._game_lifecycle_task.done():
            logger.warning("Cancelling existing game lifecycle task before starting new one")
            self._game_lifecycle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._game_lifecycle_task

        # Schedule game lifecycle as background task to avoid blocking button monitor
        self._game_lifecycle_task = asyncio.create_task(self._run_game_lifecycle(serial, source))

        # Log any exceptions from the background task
        def _log_exception(t):
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.error(f"Game lifecycle task failed: {exc}", exc_info=exc)

        self._game_lifecycle_task.add_done_callback(_log_exception)

    async def _run_game_lifecycle(self, serial: str, source: str = "controller"):
        """
        Run the full game lifecycle via StreamGameEvents.

        This function:
        1. Stops button monitor and lobby music
        2. Creates game event stream with start_config
        3. Handles all game events until game ends
        4. Restarts button monitor and lobby music when done

        Args:
            serial: Controller serial that triggered the start (for logging)
            source: Source of the start request ("controller" or "web")
        """
        from proto import game_coordinator_pb2, game_coordinator_pb2_grpc

        # Brief delay to let button monitor finish current event dispatch
        await asyncio.sleep(0.1)

        metrics.button_presses_total.labels(button="trigger", action="press").inc()

        # Determine trace context for the game:
        # - Web source: inherit current context (connects to dashboard trace)
        # - Controller/button sources: fresh context (prevents chaining to previous game)
        span_ctx = None if source == "web" else otel_context.Context()
        with tracer.start_as_current_span("start_game", context=span_ctx) as span:
            span.set_attribute("controller.serial", serial)
            span.set_attribute("game.name", self.current_selection.name)
            span.set_attribute("game.source", source)

            # Get controllers from state_manager (source of truth)
            controllers = list(self.state_manager.ready_controllers)
            span.set_attribute("controller.count", len(controllers))

            if len(controllers) < 2:
                logger.warning(f"Not enough players to start game: {len(controllers)}")
                return

            await self._prepare_game_start()

            # Build player list and config
            players = [
                game_coordinator_pb2.Player(serial=s, team=i % 2, alive=True, score=0)
                for i, s in enumerate(controllers)
            ]
            config = self._build_game_config(self.current_selection, players)

            # Inject trace context into gRPC metadata
            metadata = self._build_trace_metadata()

            # Call GameCoordinator.StreamGameEvents with start_config
            stub = game_coordinator_pb2_grpc.GameCoordinatorServiceStub(self.game_coordinator_channel)
            request = game_coordinator_pb2.StreamEventsRequest(start_config=config)

            logger.info(
                f"Starting game via GameCoordinator stream ({source}): "
                f"{self.current_selection.name} with {len(controllers)} players"
            )

            await self._run_game_event_stream(stub, request, metadata, span)

    async def _prepare_game_start(self) -> None:
        """Stop lobby services in preparation for game start."""
        self.idle_monitor.stop()

        with tracer.start_as_current_span("stop_lobby_music"):
            await self.audio.stop_lobby_music()

        with tracer.start_as_current_span("stop_button_monitor"):
            await self.stop_button_monitor()

        metrics.player_ready.clear()
        logger.info("Button monitor and lobby music stopped for game start")

        self.state = menu_pb2.MenuState.GAME_STARTING

    def _build_trace_metadata(self) -> list[tuple[str, str]]:
        """Build gRPC metadata with trace context propagation."""
        propagator = TraceContextTextMapPropagator()
        carrier: dict[str, str] = {}
        propagator.inject(carrier)
        return list(carrier.items())

    async def _run_game_event_stream(self, stub, request, metadata, span) -> None:
        """
        Run the game event stream with reconnection logic.

        Args:
            stub: GameCoordinator gRPC stub
            request: Initial StreamEventsRequest with start_config
            metadata: gRPC metadata with trace context
            span: Current tracing span
        """
        stream = None
        game_started = False
        max_reconnect_attempts = 3

        try:
            for attempt in range(max_reconnect_attempts + 1):
                try:
                    stream = self._open_game_stream(stub, request, metadata, attempt, max_reconnect_attempts)
                    result = await self._consume_game_events(stream, game_started, span)

                    if result.should_return:
                        return
                    game_started = result.game_started
                    # Stream ended normally without game_ended event
                    break

                except asyncio.CancelledError:
                    raise

                except Exception as e:
                    self._cancel_stream(stream)
                    stream = None
                    game_started = self._check_retry_eligible(e, game_started, attempt, max_reconnect_attempts)
                    backoff = attempt + 1
                    logger.warning(f"Game event stream error: {e}. Reconnecting in {backoff}s...")
                    await asyncio.sleep(backoff)

            await self._restart_lobby()

        except asyncio.CancelledError:
            logger.info("Game stream cancelled")
            raise
        except Exception as e:
            logger.error(f"Game stream error: {e}", exc_info=True)
            span.record_exception(e)
            await self._restart_lobby()
        finally:
            self._cancel_stream(stream)

    def _open_game_stream(self, stub, request, metadata, attempt: int, max_attempts: int):
        """
        Open a game event stream, using start_config on first attempt.

        Args:
            stub: GameCoordinator gRPC stub
            request: StreamEventsRequest with start_config
            metadata: gRPC metadata
            attempt: Current attempt number (0-based)
            max_attempts: Maximum reconnection attempts

        Returns:
            The gRPC stream
        """
        from proto import game_coordinator_pb2

        if attempt == 0:
            return stub.StreamGameEvents(request, metadata=metadata)

        metrics.game_event_stream_reconnects_total.inc()
        logger.warning(f"Reconnecting game event stream (attempt {attempt}/{max_attempts})")
        subscribe_request = game_coordinator_pb2.StreamEventsRequest()
        return stub.StreamGameEvents(subscribe_request, metadata=metadata)

    async def _consume_game_events(self, stream, game_started: bool, span):
        """
        Consume events from a game event stream.

        Args:
            stream: The gRPC stream to read events from
            game_started: Whether the game has already started
            span: Current tracing span

        Returns:
            _GameEventResult with updated state
        """
        from lib.types import GameEvent

        async for event in stream:
            if not game_started:
                result = await self._handle_first_game_event(event, span)
                if result.should_return:
                    return result
                game_started = True

            logger.info(f"Game event received: {event.event_type}")

            if GameEvent.is_game_ending(event.event_type):
                logger.info(f"Game ended: {event.event_type}")
                await self.event_publisher.publish(GameEvent.GAME_ENDED, event.data)
                await self._restart_lobby()
                return _GameEventResult(game_started=game_started, should_return=True)

        return _GameEventResult(game_started=game_started, should_return=False)

    async def _handle_first_game_event(self, event, span):
        """
        Handle the first event received after opening a game stream.

        Checks for start errors and marks game as started on success.

        Args:
            event: The first game event
            span: Current tracing span

        Returns:
            _GameEventResult indicating whether to return early
        """
        if event.event_type == "game_start_error":
            error = event.data.get("error", "Unknown error")
            logger.error(f"Game start failed: {error}")
            span.set_attribute("error", error)
            self.state = menu_pb2.MenuState.RUNNING
            await self._restart_lobby()
            return _GameEventResult(game_started=False, should_return=True)

        logger.info(f"Game started successfully: {event.event_type}")
        span.set_attribute("game.started", True)

        # Clear ready state now that game has started (Issue #256)
        self._clear_ready_state()
        logger.info("Ready state cleared on game start confirmation")
        return _GameEventResult(game_started=True, should_return=False)

    @staticmethod
    def _cancel_stream(stream):
        """Cancel a gRPC stream if it exists."""
        if stream is not None:
            stream.cancel()

    @staticmethod
    def _check_retry_eligible(error: Exception, game_started: bool, attempt: int, max_attempts: int) -> bool:
        """
        Check if a stream error is eligible for retry.

        Raises the error if retry is not possible.

        Args:
            error: The exception that occurred
            game_started: Whether the game has started
            attempt: Current attempt number
            max_attempts: Maximum reconnection attempts

        Returns:
            game_started value (unchanged, for caller convenience)

        Raises:
            Exception: If retry is not eligible
        """
        if not game_started:
            raise error  # NOSONAR - re-raises original exception, not generic

        if attempt >= max_attempts:
            logger.error(f"Game event stream failed after {max_attempts} attempts: {error}")
            raise error  # NOSONAR - re-raises original exception, not generic

        return game_started

    async def _restart_lobby(self):
        """Restart lobby state after game ends."""
        self.state = menu_pb2.MenuState.RUNNING

        # Start button monitor with fresh trace context (not chained to game trace)
        with tracer.start_as_current_span("restart_button_monitor", context=otel_context.Context()):
            await self.start_button_monitor()
            await self.controller_events.wait_for_connection()

        # Reset lobby state
        with tracer.start_as_current_span("reset_lobby_state"):
            # Reset state_manager and re-register controllers (sets lobby colors)
            await self.state_manager.reset()

        with tracer.start_as_current_span("restart_lobby_music"):
            await self.audio.start_lobby_music()

        # Restart idle monitor with fresh timer
        self.idle_monitor.start()

        logger.info("Lobby restarted after game")

    def _build_game_config(self, game_mode, players: list):
        """
        Build typed StartGameConfig for a game mode.

        Args:
            game_mode: Games enum value from lib.types
            players: List of Player protobuf messages

        Returns:
            StartGameConfig with appropriate mode-specific config
        """
        from proto import game_coordinator_pb2

        # Set transaction context so all flag evaluations include game session info.
        # The menu drives the REAL, player-facing game (#837), so mark it "real"
        # explicitly — this protects it from the agent's shadow-scoped experiments
        # (targeting.go scopes on game_kind != "real", #932). Explicit at the
        # real-game call site for clarity even though "real" is also the default.
        set_game_transaction_context(
            game_mode=game_mode.name,
            controller_count=len(players),
            game_kind=GAME_KIND_REAL,
        )

        # Read game settings from flagd (game domain)
        sensitivity = self.game_settings_client.get_integer_value("sensitivity", 2)

        # Build base config with the game mode's name. The menu is the REAL game
        # (#837): mark its origin so the coordinator reserves the primary slot
        # for it (and preempts any running shadow games).
        config = game_coordinator_pb2.StartGameConfig(
            game_name=game_mode.name,
            players=players,
            sensitivity=sensitivity,
            origin=game_coordinator_pb2.GAME_ORIGIN_MENU,
        )

        # Build mode-specific config using pattern matching
        match game_mode:
            case Games.JoustFFA:
                config.ffa_config.CopyFrom(game_coordinator_pb2.FFAConfig())

            case Games.JoustTeams:
                config.teams_config.CopyFrom(
                    game_coordinator_pb2.TeamsConfig(
                        num_teams=self.game_settings_client.get_integer_value("num_teams", 2),
                        random_assignment=self.game_settings_client.get_boolean_value("random_assignment", True),
                    )
                )

            case Games.JoustRandomTeams:
                config.random_teams_config.CopyFrom(
                    game_coordinator_pb2.RandomTeamsConfig(
                        num_teams=self.game_settings_client.get_integer_value("num_teams", 2),
                    )
                )

            case Games.NonStop:
                config.nonstop_config.CopyFrom(
                    game_coordinator_pb2.NonstopConfig(
                        time_limit_seconds=self.game_settings_client.get_integer_value("nonstop.time_limit_seconds", 0),
                    )
                )

            case Games.Tournament:
                config.tournament_config.CopyFrom(
                    game_coordinator_pb2.TournamentConfig(
                        invincibility_seconds=self.game_settings_client.get_float_value("invincibility_seconds", 4.0),
                    )
                )

            case Games.FightClub:
                config.fight_club_config.CopyFrom(
                    game_coordinator_pb2.FightClubConfig(
                        invincibility_seconds=self.game_settings_client.get_float_value("invincibility_seconds", 4.0),
                        min_rounds=self.game_settings_client.get_integer_value("fight_club.min_rounds", 10),
                    )
                )

            case Games.Werewolf:
                reveal_time = self.game_settings_client.get_float_value("werewolf.reveal_time_seconds", 35.0)
                config.werewolf_config.CopyFrom(
                    game_coordinator_pb2.WerewolfConfig(
                        reveal_time_seconds=reveal_time,
                    )
                )

            case Games.Zombies:
                config.zombie_config.CopyFrom(game_coordinator_pb2.ZombieConfig())

            case Games.Swapper:
                config.swapper_config.CopyFrom(game_coordinator_pb2.SwapperConfig())

            case Games.Traitor:
                config.traitor_config.CopyFrom(
                    game_coordinator_pb2.TraitorConfig(
                        num_teams=self.game_settings_client.get_integer_value("num_teams", 0),
                    )
                )

        return config
