"""Game lifecycle of the menu: start, follow the game event stream, restore the lobby (#2380).

`GameLifecycleMixin` holds what used to sit in `MenuServicer` between `_lifecycle_live` and `_build_game_config`.
It is a mixin, not a collaborator, because the methods read and write the servicer's own state (`state`,
`state_manager`, `audio`, `event_publisher`, `_game_lifecycle_task`, ...) and the tests call them on a
`MenuServicer`. Code moved as is. The logger and the tracer keep the name `services.menu.servicer`, so log
routing and the instrumentation scope do not change.
"""

import asyncio
import logging
from dataclasses import dataclass

import grpc
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from lib.feature_flags import GAME_KIND_REAL, set_game_transaction_context
from lib.telemetry import get_tracer
from lib.types import GameEvent, Games, Sound
from proto import menu_pb2
from services.menu import metrics

logger = logging.getLogger("services.menu.servicer")

tracer = get_tracer("services.menu.servicer")

# UC-010 BR-010 (#2072): bound for each lobby-restore step, so a hung gRPC call cannot keep the
# restore (and the window in which reset_menu is ignored) open for good. A recovery bound, not a
# stage knob, so a constant (C-035).
LOBBY_RESTORE_STEP_TIMEOUT_SECONDS = 10.0


async def _bounded_restore_step(step: str, run) -> None:
    """Run one lobby-restore step within LOBBY_RESTORE_STEP_TIMEOUT_SECONDS (UC-010 BR-010).

    On a timeout the step is cancelled, logged and marked on the current span: status ERROR, attributes
    `menu.restore.timed_out_step` / `menu.restore.deadline_seconds` and a `restore_step_timeout` event
    (telemetry.md §2, #2146). A success leaves the span untouched. A cancel from outside
    (menu stop, shutdown) is not a timeout: asyncio.timeout passes it on as CancelledError.
    """
    bound = LOBBY_RESTORE_STEP_TIMEOUT_SECONDS
    try:
        async with asyncio.timeout(bound) as deadline:
            await run()
    except TimeoutError:
        if not deadline.expired():
            logger.error(f"Lobby restore: {step} failed with its own TimeoutError; skipped", exc_info=True)
            return
        logger.error(f"Lobby restore: {step} did not finish within {bound}s; cancelled and skipped")
        span = trace.get_current_span()
        span.set_attribute("menu.restore.timed_out_step", step)
        span.set_attribute("menu.restore.deadline_seconds", bound)
        span.add_event("restore_step_timeout", {"menu.restore.step": step, "menu.restore.deadline_seconds": bound})
        span.set_status(trace.Status(trace.StatusCode.ERROR, f"lobby restore step '{step}' timed out after {bound}s"))


_UNREACHABLE_STATUS_CODES = (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED)


def _start_failure_reason(error: Exception) -> str:
    """The `game_start_cancelled` reason for a start that failed before the first event (UC-010 A3).

    `coordinator_unreachable` only when the coordinator could not be reached (gRPC UNAVAILABLE or
    DEADLINE_EXCEEDED, a connection error or a timeout); any other failure is `start_failed`.
    """
    if isinstance(error, grpc.RpcError) and hasattr(error, "code"):
        return "coordinator_unreachable" if error.code() in _UNREACHABLE_STATUS_CODES else "start_failed"
    if isinstance(error, (ConnectionError, TimeoutError)):
        return "coordinator_unreachable"
    return "start_failed"


@dataclass
class _GameEventResult:
    """Result from processing game events in a stream iteration."""

    game_started: bool
    should_return: bool


class GameLifecycleMixin:
    """Game start, event-stream following and lobby restore for `MenuServicer`."""

    def _lifecycle_live(self) -> bool:
        """A game is starting or running, or the lobby is being restored (UC-010 A8, UC-015 A1)."""
        return self._game_lifecycle_task is not None and not self._game_lifecycle_task.done()

    async def _reject_start_while_active(self, serial: str, source: str, game_mode: Games | None) -> None:
        """Refuse a start request while the game lifecycle task is live (UC-010 A8).

        Publishes ``game_start_rejected`` with reason ``game_active`` and clears the ready
        latch, so a later trigger can start once the task has finished. Silent while a game
        is starting or running; a short beep only in the lobby-restart window, where the
        players are back in the lobby. The live game lifecycle is left untouched.
        """
        mode = game_mode if game_mode is not None else self.current_selection
        logger.warning(f"Start from {serial} ({source}) refused: a game lifecycle is still live")
        self.ready_handler.reset_game_start_flag()
        await self.event_publisher.publish(
            "game_start_rejected",
            {"game_name": mode.name, "source": source, "reason": "game_active"},
        )
        if self._lobby_restarting:
            await self.audio.play_sound(Sound.SFX_BEEP_LOUD)

    async def _ready_start_game(self, serial: str) -> bool:
        """The lobby's ready auto start and READY trigger start (UC-010 step 4).

        UC-010 BR-014 (#2449): a STOPPED menu never auto starts. The start is dropped and logged,
        nothing is published, and the ready latch is cleared; `StartMenu` re-enables it.
        """
        if self.state == menu_pb2.MenuState.STOPPED:
            self._drop_ready_start_on_stopped_menu(serial)
            return False
        return await self._start_game(serial, ready_start=True)

    def _drop_ready_start_on_stopped_menu(self, serial: str) -> None:
        logger.info(f"Ready start from {serial} dropped: menu is stopped (UC-010 BR-014)")
        self.ready_handler.reset_game_start_flag()

    def start_menu_during_game(self) -> bool:
        """Remember a `StartMenu` that arrives while the game lifecycle is live (UC-010 A12, #2449).

        The lobby is not entered mid-game. A menu stopped during the game is marked started again:
        GAME_STARTING while the game runs (the restore then sets RUNNING), RUNNING inside the restore
        (the restore reads the state right before the idle monitor). Returns True when it undid a stop.
        """
        if self.state != menu_pb2.MenuState.STOPPED:
            return False
        self.state = menu_pb2.MenuState.RUNNING if self._lobby_restarting else menu_pb2.MenuState.GAME_STARTING
        # The lifecycle's end settles a GAME_STARTING this leaves behind (_run_game_lifecycle).
        self._start_menu_deferred = True
        return True

    async def _start_game(
        self,
        serial: str,
        source: str = "controller",
        controllers: list[str] | None = None,
        game_mode: Games | None = None,
        ready_start: bool = False,
    ) -> bool:
        """
        Schedule game start as a background task; False when it is refused (UC-010 A8).

        This is called from button event handlers, so we can't block or stop
        the button monitor synchronously. Instead, schedule the game lifecycle
        to run after a brief delay.

        Args:
            serial: Controller serial that triggered the start (for logging)
            source: Source of the start request ("controller" or "web")
            controllers: Optional explicit roster (admin force-start passes the
                force_all_start-aware list); None uses the ready controllers.
            game_mode: Mode to start, resolved by the caller (web start_game);
                None reads ``current_selection`` when the lifecycle runs.
            ready_start: The lobby's ready auto start (UC-010 BR-014: dropped on a STOPPED menu).
        """
        # UC-010 A8 (#2054): a live lifecycle task is a game that is starting or running. Refuse
        # the start; never cancel the live game's stream. A finished task needs no cancel.
        if self._lifecycle_live():
            await self._reject_start_while_active(serial, source, game_mode)
            return False

        # Schedule game lifecycle as background task to avoid blocking button monitor
        self._game_lifecycle_task = asyncio.create_task(
            self._run_game_lifecycle(serial, source, controllers, game_mode=game_mode, ready_start=ready_start)
        )

        # Log any exceptions from the background task
        def _log_exception(t):
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.error(f"Game lifecycle task failed: {exc}", exc_info=exc)

        self._game_lifecycle_task.add_done_callback(_log_exception)
        return True

    async def _run_game_lifecycle(
        self,
        serial: str,
        source: str = "controller",
        controllers: list[str] | None = None,
        game_mode: Games | None = None,
        ready_start: bool = False,
    ):
        """Run the game lifecycle (`_run_game_lifecycle_steps`) and settle the menu state afterwards.

        UC-010 A12 (#2449): however the lifecycle ends (game end, failed start, roster abort), no game
        runs any more, so a GAME_STARTING left behind by a StartMenu that undid a StopMenu after the
        last state change becomes RUNNING with the idle monitor. A cancel (reset_menu, shutdown) is left
        to its canceller.
        """
        self._start_menu_deferred = False
        cancelled = False
        try:
            await self._run_game_lifecycle_steps(
                serial, source, controllers, game_mode=game_mode, ready_start=ready_start
            )
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if not cancelled and self._start_menu_deferred and self.state == menu_pb2.MenuState.GAME_STARTING:
                logger.info("Game lifecycle ended with the menu in GAME_STARTING; menu set RUNNING")
                self.state = menu_pb2.MenuState.RUNNING
                self.idle_monitor.start()

    async def _run_game_lifecycle_steps(
        self,
        serial: str,
        source: str = "controller",
        controllers: list[str] | None = None,
        game_mode: Games | None = None,
        ready_start: bool = False,
    ):
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
            controllers: Optional explicit roster (admin force-start passes the
                force_all_start-aware list); None uses the ready controllers.
            game_mode: Mode resolved by the caller. A web start_game passes it so a
                lobby SELECT during the delay cannot swap the mode (#1278).
            ready_start: The lobby's ready auto start; dropped when a StopMenu landed meanwhile
                (UC-010 BR-014).
        """
        from proto import game_coordinator_pb2, game_coordinator_pb2_grpc

        # Brief delay to let button monitor finish current event dispatch
        await asyncio.sleep(0.1)
        if ready_start and self.state == menu_pb2.MenuState.STOPPED:
            # UC-010 BR-014: a StopMenu during the ready feedback delay also stops the auto start.
            self._drop_ready_start_on_stopped_menu(serial)
            return
        mode = game_mode if game_mode is not None else self.current_selection

        # Determine trace context for the game:
        # - Web source: inherit current context (connects to dashboard trace)
        # - Controller/button sources: fresh context (prevents chaining to previous game)
        span_ctx = None if source == "web" else otel_context.Context()
        with tracer.start_as_current_span("start_game", context=span_ctx) as span:
            span.set_attribute("controller.serial", serial)
            span.set_attribute("game.name", mode.name)
            span.set_attribute("game.source", source)

            # Roster: an explicit list (admin force-start, force_all_start-aware)
            # overrides the default ready-controllers source of truth.
            roster = list(controllers) if controllers is not None else list(self.state_manager.ready_controllers)
            span.set_attribute("controller.count", len(roster))

            # UC-010 BR-001 (#1862): every start path (ready auto start, trigger,
            # admin force-start, web / ProcessInput) needs the mode's minimum.
            minimum_players = mode.minimum_players
            if len(roster) < minimum_players:
                logger.warning(f"Not enough players to start {mode.name}: {len(roster)} (minimum {minimum_players})")
                # Don't leave the menu stuck in GAME_STARTING if a start aborts.
                if self.state == menu_pb2.MenuState.GAME_STARTING:
                    self.state = menu_pb2.MenuState.RUNNING
                    # A RUNNING menu has its idle monitor (idempotent; UC-010 A12: a StartMenu that
                    # undid a stop before this abort leaves no idle monitor running otherwise).
                    self.idle_monitor.start()
                # Publish on every roster abort (UC-010 A3) — the ready/auto-start
                # path is still RUNNING here, only admin force-start is GAME_STARTING.
                await self.event_publisher.publish(
                    "game_start_cancelled",
                    {"game_name": mode.name, "player_count": len(roster), "minimum_players": minimum_players},
                )
                await self.audio.play_voice(Sound.MENU_VOX_NOT_ENOUGH_PLAYERS)
                # Restore lobby LEDs. The button monitor and lobby music are still
                # running (we abort before _prepare_game_start), so a full
                # _restart_lobby would be wrong here — but admin force-start sends a
                # white flash before this path, so re-register controllers to set
                # the proper lobby colors back. state_manager.reset() re-runs the
                # CONNECTED on_enter handlers which set lobby colors.
                await self.state_manager.reset()
                return

            if not await self._prepare_game_start():
                return

            # _prepare_game_start() has already set state=GAME_STARTING and torn
            # down the lobby (button monitor + lobby music). If anything in the
            # prep/build window below throws (bad mode config, stub creation,
            # audio/button RPC error), restore the lobby and reset state instead
            # of leaving the menu stuck in GAME_STARTING with no lobby. The
            # exception otherwise only surfaces in the background task's
            # done-callback. _run_game_event_stream already self-protects, so this
            # only needs to cover the synchronous prep/build window before it.
            try:
                # Build player list and config
                players = [
                    game_coordinator_pb2.Player(serial=s, team=i % 2, alive=True, score=0) for i, s in enumerate(roster)
                ]
                config = self._build_game_config(mode, players)

                # Inject trace context into gRPC metadata
                metadata = self._build_trace_metadata()

                # Call GameCoordinator.StreamGameEvents with start_config
                stub = game_coordinator_pb2_grpc.GameCoordinatorServiceStub(self.game_coordinator_channel)
                request = game_coordinator_pb2.StreamEventsRequest(start_config=config)

                logger.info(
                    f"Starting game via GameCoordinator stream ({source}): {mode.name} with {len(roster)} players"
                )

                await self._run_game_event_stream(stub, request, metadata, span)
            except Exception as e:
                logger.error(f"Game start failed during preparation: {e}", exc_info=True)
                span.record_exception(e)
                # UC-010 A11 (#2379): a menu stopped meanwhile still needs its button stream back.
                # A restore that already ran (and failed) is not repeated (UC-010 A3).
                if not self._lobby_restore_started and self.state in (
                    menu_pb2.MenuState.GAME_STARTING,
                    menu_pb2.MenuState.STOPPED,
                ):
                    await self._restart_lobby()
                raise

    async def _prepare_game_start(self) -> bool:
        """Stop lobby services in preparation for game start.

        Returns False when the menu was stopped meanwhile (UC-010 A11 step 3, #2379): the start is
        aborted, the lobby is restored with the menu left STOPPED, and no game is requested.
        """
        self._lobby_restore_started = False
        # A start on a menu that was already STOPPED is #2449 (out of scope here); only a StopMenu
        # that lands during the awaits below aborts the start.
        stopped_before = self.state == menu_pb2.MenuState.STOPPED
        self.idle_monitor.stop()

        with tracer.start_as_current_span("stop_lobby_music"):
            await self.audio.stop_lobby_music()

        with tracer.start_as_current_span("stop_button_monitor"):
            await self.stop_button_monitor()

        metrics.player_ready.clear()
        logger.info("Button monitor and lobby music stopped for game start")

        if not stopped_before and self.state == menu_pb2.MenuState.STOPPED:
            # StopMenu landed during the awaits above: GAME_STARTING must not overwrite it.
            logger.info("Menu stopped during game start preparation; start aborted")
            await self._restart_lobby()
            return False

        self.state = menu_pb2.MenuState.GAME_STARTING
        return True

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
        # UC-010 A13 (#2450): the start was confirmed and every reconnect failed.
        coordinator_lost = False
        max_reconnect_attempts = 3
        self._followed_game_id = None

        try:
            for attempt in range(max_reconnect_attempts + 1):
                try:
                    stream = self._open_game_stream(stub, request, metadata, attempt, max_reconnect_attempts)
                    if attempt > 0 and await self._followed_game_gone(stub, stream):
                        await self._end_followed_game_while_disconnected()
                        return
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
                    # A stream that breaks mid-iteration never returns its result: the confirmed
                    # start (BR-006) is what makes the break eligible for a reconnect.
                    game_started = game_started or self._followed_game_id is not None
                    # A restore that already ran (the game ended, then its restore raised) is no lost coordinator.
                    coordinator_lost = (
                        game_started and attempt >= max_reconnect_attempts and not self._lobby_restore_started
                    )
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
            try:
                # A restore that already ran (and raised) is not repeated (UC-010 A3).
                if not self._lobby_restore_started:
                    await self._restart_lobby()
            finally:
                if self._followed_game_id is None:
                    # UC-010 A3 (#2379): the start failed before the coordinator's first event; the
                    # cancel goes out even when the restore raises (the exception still propagates).
                    await self.event_publisher.publish(
                        "game_start_cancelled",
                        {"game_name": request.start_config.game_name, "reason": _start_failure_reason(e)},
                    )
                elif coordinator_lost:
                    # UC-010 A13 (#2450): the menu no longer follows the started game; this goes out
                    # after the restore, also when the restore raises.
                    await self.event_publisher.publish(
                        GameEvent.GAME_ENDED,
                        {"game_id": self._followed_game_id, "reason": "coordinator_lost"},
                    )
        finally:
            self._cancel_stream(stream)

    async def _followed_game_gone(self, stub, stream) -> bool:
        """Whether the followed game ended while the stream was down (UC-010 A9, BR-009).

        Waits for the reconnected stream's headers first: the coordinator sends them once the
        subscription is registered (#1613), so a game that ends after this check still delivers
        its terminal event on the new stream. A header wait that times out raises (a normal
        reconnect failure). grpc.aio returns empty headers for an unavailable coordinator and the
        error only surfaces when the stream is iterated: the state lookup then fails, the stream is
        kept, and the iteration error drives the retry. A failed or unsuccessful lookup never
        counts as "game gone".
        """
        from proto import game_coordinator_pb2

        await asyncio.wait_for(stream.initial_metadata(), timeout=5.0)
        try:
            response = await stub.GetGameState(game_coordinator_pb2.GetGameStateRequest(), timeout=2.0)
        except Exception as e:
            logger.warning(f"GetGameState after reconnect failed, keeping the stream: {e}")
            return False
        if not response.success:
            logger.warning(f"GetGameState after reconnect unsuccessful, keeping the stream: {response.error}")
            return False
        info = response.game_info
        if info.state not in (game_coordinator_pb2.GameState.STARTING, game_coordinator_pb2.GameState.RUNNING):
            return True
        return bool(self._followed_game_id) and info.game_id != self._followed_game_id

    async def _end_followed_game_while_disconnected(self) -> None:
        """Finish the lifecycle of a game that ended while the stream was down (UC-010 A9)."""
        from lib.types import GameEvent

        logger.warning(f"Game {self._followed_game_id} ended while the game event stream was down")
        await self.event_publisher.publish(
            GameEvent.GAME_ENDED,
            {"game_id": self._followed_game_id or "", "reason": "ended_while_disconnected"},
        )
        await self._restart_lobby()

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

            # High-rate, droppable events (the hold energy bar, UC-035 BR-008) log at DEBUG.
            log = logger.debug if event.event_type == GameEvent.PLAYER_SHIELD_ENERGY else logger.info
            log(f"Game event received: {event.event_type}")

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
            await self._restart_lobby()
            return _GameEventResult(game_started=False, should_return=True)

        logger.info(f"Game started successfully: {event.event_type}")
        span.set_attribute("game.started", True)
        # UC-010 BR-009: the game a reconnect must find again.
        self._followed_game_id = event.game_id

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
        """Restart lobby state after game ends.

        UC-010 BR-010: every step is bounded; a step that times out is logged and skipped, so the
        restore always ends and the idle monitor always starts. UC-010 A11 (#2379): a menu stopped
        during the game stays STOPPED and gets no idle monitor; the rest of the restore still runs.
        UC-010 A12 (#2449): a later StartMenu during the game undoes that stop (last one wins).
        """

        async def start_button_monitor_and_wait() -> None:
            await self.start_button_monitor()
            await self.controller_events.wait_for_connection()

        # UC-010 A8: a start refused in this window beeps (the players are back in the lobby).
        self._lobby_restarting = True
        self._lobby_restore_started = True
        try:
            if self.state != menu_pb2.MenuState.STOPPED:
                self.state = menu_pb2.MenuState.RUNNING

            # Start button monitor with fresh trace context (not chained to game trace)
            with tracer.start_as_current_span("restart_button_monitor", context=otel_context.Context()):
                await _bounded_restore_step("button monitor", start_button_monitor_and_wait)

            # Reset lobby state
            with tracer.start_as_current_span("reset_lobby_state"):
                # Reset state_manager and re-register controllers (sets lobby colors)
                await _bounded_restore_step("controller re-registration", self.state_manager.reset)

            with tracer.start_as_current_span("restart_lobby_music"):
                await _bounded_restore_step("lobby music", self.audio.start_lobby_music)

            # Restart idle monitor with fresh timer. The state is read here, not at the start: a
            # StopMenu during the restore steps above must stick too (UC-010 A11 step 3, BR-010).
            menu_stopped = self.state == menu_pb2.MenuState.STOPPED
            if not menu_stopped:
                self.idle_monitor.start()
        finally:
            self._lobby_restarting = False

        logger.info("Lobby restored after game" + (" (menu stays stopped)" if menu_stopped else ""))

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

        # UC-012 BR-001: settings are read once here, at game start; the running game keeps them.
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
