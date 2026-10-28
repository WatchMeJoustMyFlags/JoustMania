"""Menu gRPC Servicer for JoustMania."""

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass

from openfeature.flag_evaluation import Reason
from openfeature.provider.no_op_provider import PASSED_IN_DEFAULT
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from lib.feature_flags import (
    GAME_KIND_REAL,
    get_flag_client,
    init_flag_domain,
    set_game_transaction_context,
)
from lib.flag_service_writer import FlagServiceWriter
from lib.setting_ladders import AUTOMATIC, AUTOMATIC_OPTIONS, SHIELD_KNOB_LADDER
from lib.telemetry import get_tracer
from lib.types import Games, Sound
from proto import menu_pb2, menu_pb2_grpc
from services.menu import metrics
from services.menu.controller_events import ControllerEventLoop
from services.menu.event_publisher import EventPublisher
from services.menu.handlers import AdminModeHandler, ConnectedHandler, IdleHandler, ReadyHandler
from services.menu.handlers.base import ControllerState
from services.menu.idle_monitor import IdleMonitor, read_sentinel_animation
from services.menu.settings_ops import (
    AGENT_ENABLED_FLAG,
    AGENT_POLICY_FLAG,
    AGENT_WRITE_GATE_FLAG,
    BAD_OPTION,
    DECISION_BACKEND_FLAG,
    GAME_ACTIVE,
    LOBBY_ONLY_OPTIONS,
    OK,
    SHIELD_ACTIVATION_FLAG,
    STALE,
    Result,
    SettingsOps,
    is_failure,
)
from services.menu.state_manager import StateManager
from services.menu.utils import AudioHelper, LedController
from services.menu.utils.audio import GAME_MODE_VOICE
from services.menu.utils.settings import DEFAULT_GAME_MODE, DEFAULT_VOICE_ACTOR, GAME_MODES, get_next_game_mode

logger = logging.getLogger(__name__)

# Lazy telemetry initialization - defers OTLP setup until first span
tracer = get_tracer(__name__)

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


def _variant(details) -> str:
    """The resolved variant name of an OpenFeature evaluation, empty when the client fell back to the default."""
    return details.variant or ""


def _enabled_state(details) -> str:
    """`on`, `off` or `unknown` for an agent on/off flag's evaluation (services.md §3.1, UC-015 BR-013, #2101).

    Used for the kill switch `enabled` and the write gate `interventions_enabled` (#2093).

    Unknown is any evaluation that did not come from a resolved flag: an error code, reason ERROR
    (provider not ready, flag not found, ...), no variant (the client fell back to the default),
    or the no-op provider's passed-in default before a provider is set.
    """
    if details.error_code is not None or details.reason == Reason.ERROR:
        return "unknown"
    if not details.variant or details.variant == PASSED_IN_DEFAULT:
        return "unknown"
    return "on" if details.value is True else "off"


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
        # Selectable game modes, exposed to the admin handler so controller Cross
        # cycling and the dashboard share one source of truth (#1030). GAME_MODES
        # already excludes unimplemented/meta modes.
        self.game_options: list[str] = list(GAME_MODES)

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

        # Initialize flagd domains for game, user, system, and agent settings
        init_flag_domain("game")
        init_flag_domain("user")
        init_flag_domain("system")
        # The agent domain is read for the truthful admin display (#818) and
        # written for the on-device agent kill-switch (#819).
        init_flag_domain("agent")

        # Admin settings and the menu selection are persisted admin values on the
        # flag service (#1415, UC-053 A2): PUT /game|user/{flag} {"persist": true}.
        # The flag files are the read-only baseline (C-033).
        self.game_settings_writer = FlagServiceWriter("game")
        self.user_prefs_writer = FlagServiceWriter("user")
        # Agent policy writer for the on-device kill-switch (#819). The human
        # owns agent policy per docs/OWNERSHIP_MODEL.md §2/§6.2. The agent set is
        # served by the flag service (#1412): the kill switch is a persisted admin
        # setting (PUT /agent/interventions_allowed {"persist": true}, FR-019, D10).
        self.agent_settings_writer = FlagServiceWriter("agent")
        # The operator pad's kill switch and write gate (#2050, #2093, UC-015 BR-001): agent/enabled and
        # agent/interventions_enabled have no persist in the registry (a persisted write is 400) and
        # must not survive a restart (UC-045 step 7).
        self.agent_enabled_writer = FlagServiceWriter("agent", persist=False)

        # OpenFeature clients for reading settings
        self.game_settings_client = get_flag_client("game")
        self.user_prefs_client = get_flag_client("user")
        self.system_client = get_flag_client("system")
        # Client for resolving effective (agent-override-aware) values for the
        # admin display (#818) and the kill-switch level (#819).
        self.agent_settings_client = get_flag_client("agent")

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
            agent_settings_writer=self.agent_settings_writer,
            agent_settings_client=self.agent_settings_client,
            publish_event=self.event_publisher.publish,
            select_game_mode=self.select_game_mode,
        )

        # Register handlers with state manager
        self.connected_handler = ConnectedHandler()
        self.ready_handler = ReadyHandler(start_game_callback=self._start_game)
        self.state_manager.register_handler(self.connected_handler)
        self.state_manager.register_handler(self.ready_handler)

        # Voice profile (flag menu_voice: gen_female or gen_male)
        self.voice_actor = DEFAULT_VOICE_ACTOR

        # Settings core shared by admin mode and the operator pad commands (#2050, UC-015).
        self.settings_ops = SettingsOps(
            get_state_manager=lambda: self.state_manager,
            controller_channel=self.controller_channel,
            metrics=metrics,
            callbacks=self,
            agent_enabled_writer=self.agent_enabled_writer,
        )

        # Admin mode handler
        self.admin_handler = AdminModeHandler(
            controller_channel=self.controller_channel,
            tracer=tracer,
            callbacks=self,
            metrics=metrics,
            settings_ops=self.settings_ops,
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

        # Track game lifecycle task for clean shutdown. Every non-None assignment counts one
        # lifecycle start; lifecycle_generation derives from that count (UC-015 A5).
        self._lifecycle_starts = 0
        self._lifecycle_task: asyncio.Future | None = None
        self._game_lifecycle_task = None
        # True while the lifecycle task restores the lobby after a game (UC-010 A8 lobby-restart window).
        self._lobby_restarting = False
        # game_id of the game the lifecycle follows once its start is confirmed; None before (UC-010 BR-009).
        self._followed_game_id: str | None = None

        logger.info("Menu service initialized")

    # Game lifecycle task and its generation (UC-015 A5, services.md §3.1)

    @property
    def _game_lifecycle_task(self) -> asyncio.Future | None:
        """The game lifecycle task: a game starting or running, or a lobby restore."""
        return self._lifecycle_task

    @_game_lifecycle_task.setter
    def _game_lifecycle_task(self, task: asyncio.Future | None) -> None:
        # The start count moves in the same statement as the task, so no read sees one without the other.
        if task is not None:
            self._lifecycle_starts += 1
        self._lifecycle_task = task

    @property
    def lifecycle_generation(self) -> int:
        """Goes up when a lifecycle task starts and again when it ends (the lobby is back).

        Derived at read time from the start count and the task's own done state, so a snapshot never
        pairs a lifecycle with an older generation: the end counts in the same event-loop step as the
        task finishing, before any done callback, also for a task cancelled before it ran. Odd while
        live, even while idle; starts at 0 with the menu process.
        """
        return 2 * self._lifecycle_starts - (1 if self._lifecycle_live() else 0)

    # AdminModeCallbacks protocol implementation

    def set_menu_state(self, state) -> None:
        """Set the menu state (AdminModeCallbacks)."""
        self.state = state

    def get_game_options(self) -> list[str]:
        """Get list of available game options (AdminModeCallbacks)."""
        return list(self.game_options)

    async def select_game_mode(self, game_name: str, *, source: str = "admin", serial: str | None = None) -> bool:
        """Select a game mode by name (AdminModeCallbacks, StateManager callback).

        The single selection path (#1272, #1285): admin Cross, lobby SELECT,
        ``ProcessInput`` select and the web ``select_game`` / ``start_game``
        commands all land in the SAME state the coordinator start path reads
        (#1030): the servicer's ``current_selection``, the state_manager's game
        mode (the value the admin handler displays), the lobby colour of every
        CONNECTED / READY controller (UC-011 BR-005), a ``selection_changed``
        event and the ``current_game`` default persisted to flagd.

        Returns False (and changes nothing) for an unknown or unplayable mode.
        """
        game_mode = Games.from_name(game_name)
        if game_mode is None or game_name not in GAME_MODES:
            logger.warning(f"Unknown game mode selected via {source}: {game_name}")
            return False
        self.current_selection = game_mode
        self.state_manager.set_game_mode(game_mode)
        # UC-011 step 6, BR-005 (#2105): every lobby controller takes the new mode's colour.
        await self.state_manager.show_lobby_colors()
        payload = {"game_name": game_mode.name, "source": source}
        if serial is not None:
            payload["serial"] = serial
        await self.event_publisher.publish("selection_changed", payload)
        await asyncio.to_thread(self.user_prefs_writer.update_default_variant, "current_game", game_mode.name)
        logger.info(f"Game selected via {source}: {game_mode.name}")
        return True

    async def start_game(self, serial: str, source: str, controllers: list[str] | None = None) -> None:
        """Start a coordinator game (AdminModeCallbacks).

        Lets the admin force-start path launch the selected mode through the same
        coordinator start path the ready/web flow uses (#1029), optionally with an
        explicit controller roster (admin force-start honors force_all_start).
        """
        await self._start_game(serial, source=source, controllers=controllers)

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

    async def on_button(self, serial: str, button: str, is_press: bool, event_time: float | None = None) -> None:
        """Handle button event (ControllerEventCallbacks)."""
        # Reset idle timer on any button activity
        self.idle_monitor.reset_activity()

        # Check for admin mode combo (all 4 face buttons pressed)
        button_state = self.state_manager.button_states.get(serial, {})
        if is_press and button in ["cross", "circle", "square", "triangle"]:
            preview_state = dict(button_state)
            preview_state[button] = True
            if self.admin_handler.check_combo_from_state(preview_state):
                if self.state != menu_pb2.MenuState.RUNNING:
                    # UC-012 Pre-1: admin mode only from a RUNNING menu (the button
                    # stream already runs before StartMenu and after StopMenu).
                    logger.info(f"Admin combo from {serial} ignored: menu is not running")
                    await self.state_manager.handle_button_event(serial, button, is_press, event_time)
                    return
                self.admin_handler.release_stale_session()
                if self.admin_handler.is_held_by_other(serial):
                    # UC-012 BR-004: only one admin at a time. Ignore the
                    # combo and handle the press as an ordinary button.
                    logger.info(
                        f"Admin combo from {serial} ignored: admin mode already active on "
                        f"{self.admin_handler.controller_serial}"
                    )
                    await self.state_manager.handle_button_event(serial, button, is_press, event_time)
                    return
                if not self.admin_handler.combo_shown:
                    self.admin_handler.combo_shown = True
                    from services.menu.handlers.base import ControllerState

                    await self.state_manager.transition_to(serial, ControllerState.ADMIN)
                await self.state_manager.handle_button_event(serial, button, is_press, event_time)
                return

        # Reset admin combo flag when any face button released
        if not is_press and button in ["cross", "circle", "square", "triangle"]:
            self.admin_handler.combo_shown = False

        await self.state_manager.handle_button_event(serial, button, is_press, event_time)

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

            await self.enter_lobby()

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

    async def enter_lobby(self) -> None:
        """Enter the lobby: the one lobby-entry path for StartMenu and the boot auto-start (D-M2, #1505).

        Sets RUNNING, un-readies everyone, restores the persisted ``current_game``
        (an unplayable value falls back to JoustFFA), starts lobby music and the
        idle monitor, and publishes ``menu_started``.
        """
        self.state = menu_pb2.MenuState.RUNNING
        self._clear_ready_state()

        # Load settings from flagd (user domain)
        self.voice_actor = self.user_prefs_client.get_string_value("menu_voice", DEFAULT_VOICE_ACTOR)
        current_game_name = self.user_prefs_client.get_string_value("current_game", DEFAULT_GAME_MODE.name)
        game = Games.from_name(current_game_name)
        self.current_selection = game if game and game.name in GAME_MODES else DEFAULT_GAME_MODE
        self.state_manager.set_game_mode(self.current_selection)
        # UC-010 A11 (#2175): every controller the menu knows is CONNECTED again and shows the restored
        # mode's dim lobby colour; bounded like the post-game restore (BR-010).
        await _bounded_restore_step("controller re-registration", self.state_manager.reset)

        await _bounded_restore_step("lobby music", self.audio.start_lobby_music)
        self.idle_monitor.start()
        await self.event_publisher.publish("menu_started", {})

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

            # UC-010 A11 (#2175): un-ready the lobby but keep the roster. The button stream stays open
            # and the controller manager re-sends no connect events, so a cleared roster would let a
            # subset of the controllers auto start after the next StartMenu (BR-001, step 4).
            self._clear_ready_state()
            self.state_manager.return_all_to_connected()
            # UC-012 BR-004: no controller is in ADMIN any more, so release the single-admin lock.
            self.admin_handler.abandon_session()

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

            if input_type == "web_command" and data.get("command", "") in self.PAD_COMMANDS:
                success, error = await self._handle_pad_command(data, span)
                status = "ok" if success else "error"
                metrics.grpc_requests_total.labels(method="ProcessInput", status=status).inc()
                return menu_pb2.ProcessInputResponse(success=success, error=error)
            rejection = None
            if input_type == "button_press":
                rejection = await self._handle_button_input(data, span)
            elif input_type == "web_command":
                rejection = await self._handle_web_command(data, span)
            elif input_type == "reset_menu":
                await self._handle_reset_menu()
            if rejection is not None:
                metrics.grpc_requests_total.labels(method="ProcessInput", status="error").inc()
                return menu_pb2.ProcessInputResponse(success=False, error=rejection)

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

    def read_operator_settings_at_start(self) -> None:
        """Boot: read the operator-set values once in the background (UC-015 BR-018), so the first
        K6 hold after a menu restart starts from a known value instead of a silent preview."""
        self.settings_ops.schedule_operator_refresh()

    async def GetMenuStatus(self, _request, _context):
        """Read-only menu snapshot (services.md §3.1): menu memory and in-process flag clients only.

        Never the HTTP flag writer (0.5 s per flag). A flag the client cannot resolve reads as its
        default: empty variant names, and the kill switch as off with `agent_enabled_state` unknown.
        """
        start_time = time.time()
        try:
            # UC-015 BR-018: the operator's shield value is read in the background, never in this call.
            self.settings_ops.schedule_operator_refresh()
            response = self._menu_status()
        except Exception:
            metrics.grpc_requests_total.labels(method="GetMenuStatus", status="error").inc()
            raise
        finally:
            metrics.grpc_request_duration_seconds.labels(method="GetMenuStatus").observe(time.time() - start_time)
        metrics.grpc_requests_total.labels(method="GetMenuStatus", status="ok").inc()
        return response

    def _menu_status(self) -> menu_pb2.GetMenuStatusResponse:
        """Build the GetMenuStatus snapshot (synchronous: in-process reads only)."""
        game = self.game_settings_client
        user = self.user_prefs_client
        agent = self.agent_settings_client
        enabled_state = _enabled_state(agent.get_boolean_details(AGENT_ENABLED_FLAG, False))
        return menu_pb2.GetMenuStatusResponse(
            menu_state=self.state,
            lifecycle_live=self._lifecycle_live(),
            lifecycle_generation=self.lifecycle_generation,
            selection=self.current_selection.name,
            ready_count=self.ready_controller_count,
            connected_count=len(self.connected_controllers),
            admin_serial=(self.admin_handler.admin_controller or "") if self.admin_handler.is_active else "",
            agent_enabled=enabled_state == "on",
            agent_enabled_state=enabled_state,
            agent_interventions_enabled_state=_enabled_state(agent.get_boolean_details(AGENT_WRITE_GATE_FLAG, False)),
            agent_level=_variant(agent.get_string_details(AGENT_POLICY_FLAG, "")),
            agent_saved_level=self.settings_ops.saved_agent_level or "",
            agent_backend=_variant(agent.get_string_details(DECISION_BACKEND_FLAG, "")),
            sensitivity=game.get_integer_value("sensitivity", 2),
            num_teams=game.get_integer_value("num_teams", 2),
            instructions=bool(user.get_boolean_value("game_instructions", False)),
            force_all_start=bool(game.get_boolean_value("force_all_start", False)),
            audio_volume_master=_variant(user.get_float_details("audio_volume.master", 1.0)),
            shield_activation=_variant(game.get_string_details(SHIELD_ACTIVATION_FLAG, "")),
            operator_settings=dict(self.settings_ops.operator_settings),
        )

    async def ListGameModes(self, _request, _context):
        """List the playable game modes (UC-011 BR-001) for the dashboard selector."""
        metrics.grpc_requests_total.labels(method="ListGameModes", status="ok").inc()
        return menu_pb2.ListGameModesResponse(game_modes=list(GAME_MODES), default_mode=DEFAULT_GAME_MODE.name)

    async def StreamMenuEvents(self, _request, context):
        """Stream menu events in real-time."""
        subscriber_id = f"menu_events_{time.time()}"
        metrics.stream_connections_active.inc()

        span = trace.get_current_span()
        span.set_attribute("subscriber.id", subscriber_id)

        event_queue = await self.event_publisher.subscribe(subscriber_id)

        try:
            # Headers now = "subscriber registered" (#1613); inside the try so a client that leaves
            # during the send still runs the unsubscribe below.
            await context.send_initial_metadata(())
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

    async def _handle_button_input(self, data: dict, span) -> str | None:
        """Handle button_press input type; the error code when the start is refused (UC-010 A8), else None."""
        button = data.get("button", "")
        span.set_attribute("button", button)

        if button == "trigger":
            if not await self._start_game(serial="process_input", source="button_input"):
                return GAME_ACTIVE

        elif button == "select":
            next_mode = get_next_game_mode(self.current_selection)
            await self.select_game_mode(next_mode.name, source="button_input")
        return None

    async def _handle_web_command(self, data: dict, span) -> str | None:
        """Handle web_command input type.

        Returns an error message when the command is rejected, else None.
        """
        command = data.get("command", "")
        span.set_attribute("command", command)

        if command == "start_game":
            requested_mode = data.get("mode", "")
            game_mode = None
            if requested_mode:
                game_mode = Games.from_name(requested_mode)
                if game_mode is None or game_mode.name not in GAME_MODES:
                    return await self._reject_web_start_mode(requested_mode, span)
                if self._lifecycle_live():
                    # UC-010 A8 (#2177 review): a refused start changes nothing, not even the selection.
                    await self._reject_start_while_active("web", "web", game_mode)
                    return GAME_ACTIVE
                await self.select_game_mode(game_mode.name, source="web")
            if not await self._start_game(serial="web", source="web", game_mode=game_mode):
                # UC-010 A8, UC-015 A1 (#2176): the caller sees the refusal, not success=true.
                return GAME_ACTIVE

        elif command == "select_game":
            game_name = data.get("game_name", "")
            if await self.select_game_mode(game_name, source="web"):
                # UC-011 A1 continues at step 5: both selection events, like admin Cross.
                selected_name = Games.from_name(game_name).name
                await self.event_publisher.publish(
                    "game_selection_changed", {"game_name": selected_name, "source": "web"}
                )
                voice_file = GAME_MODE_VOICE.get(game_name)
                if voice_file:
                    await self.audio.play_voice(voice_file)
        return None

    # Operator pad commands (#2050, UC-015, services.md §3.1): no serial, no LEDs, error codes.
    PAD_COMMANDS = frozenset({"force_start", "step_setting", "preview_setting", "agent_switch", "set_backend"})

    async def _handle_pad_command(self, data: dict, span) -> tuple[bool, str]:
        """Run one pad command; (success, error code). A failed write plays the failure cue (BR-009)."""
        command = data.get("command", "")
        span.set_attribute("command", command)
        if command == "force_start":
            result = await self._pad_force_start(data, span)
        elif command == "step_setting":
            result = await self._pad_step_setting(data)
        elif command == "preview_setting":
            result = await self._pad_preview_setting(data)
        elif command == "agent_switch":
            direction = data.get("direction", "")
            result = await self.settings_ops.agent_switch(direction)
            if result.error == OK:
                await self.settings_ops.announce("agent_switch", direction)
        else:
            result = await self.settings_ops.set_backend(data.get("backend", ""))
        error = result.error or ""
        if error and error != OK:
            span.set_attribute("error", error)
            logger.warning(f"Pad command {command} refused: {error}")
        if is_failure(error):
            # UC-015 A3: fail closed, never queued or retried; the host hears two loud beeps.
            await self.settings_ops.play_failure_cue()
        return error in ("", OK), error

    async def _pad_step_setting(self, data: dict) -> Result:
        """step_setting: lobby-only refusal (UC-015 A1), then one clamped write (A7, BR-005)."""
        option = data.get("option", "")
        try:
            steps = int(data.get("steps", "1") or "1")
        except ValueError:
            return Result(None, None, BAD_OPTION)
        if option in LOBBY_ONLY_OPTIONS and self._lifecycle_live():
            return Result(None, None, GAME_ACTIVE)
        result = await self.settings_ops.step_setting(option, data.get("direction", ""), steps)
        if result.error is None:
            await self.settings_ops.announce(option, result.new)
        return result

    async def _pad_preview_setting(self, data: dict) -> Result:
        """preview_setting: the K1 / K6 live preview (UC-015 BR-017, #2106, #2215); a pulse, nothing written.

        Writes no flag, changes no selection and plays no voice: only the BR-015 pulse of the mode's
        colour (or the BR-018 pulse of the shield mode's) on every lobby controller. Refused like a
        step while the lifecycle is live (A1).
        """
        option = data.get("option", "")
        if option == SHIELD_ACTIVATION_FLAG:
            shield_mode = data.get("mode", "")
            if shield_mode not in SHIELD_KNOB_LADDER:
                return Result(None, None, BAD_OPTION)
            if self._lifecycle_live():
                return Result(None, None, GAME_ACTIVE)
            await self.settings_ops.show_shield_mode_on_lobby(shield_mode)
            return Result(None, shield_mode)
        if option in AUTOMATIC_OPTIONS:
            # K2 / K5 (#2245): only the `automatic` rung has an LED cue; the values have none.
            if data.get("mode", "") != AUTOMATIC:
                return Result(None, None, BAD_OPTION)
            if self._lifecycle_live():
                return Result(None, None, GAME_ACTIVE)
            await self.settings_ops.show_automatic_on_lobby()
            return Result(None, AUTOMATIC)
        if option != "current_game":
            return Result(None, None, BAD_OPTION)
        mode = Games.from_name(data.get("mode", ""))
        if mode is None or mode.name not in GAME_MODES:
            return Result(None, None, BAD_OPTION)
        if self._lifecycle_live():
            return Result(None, None, GAME_ACTIVE)
        await self.settings_ops.show_mode_on_lobby(mode.name)
        return Result(None, mode.name)

    async def _pad_force_start(self, data: dict, span) -> Result:
        """force_start: UC-014 steps 3-5 with the live roster and no host serial (UC-015 BR-008, A2).

        A non-empty ``expected_generation`` that differs from ``lifecycle_generation`` (or is not an
        integer) is refused with ``stale`` before anything changes, and again after the roster lookup
        (UC-015 A5).
        """
        expected = data.get("expected_generation", "")
        if self._generation_stale(expected):
            return Result(None, None, STALE)
        if self._lifecycle_live():
            return Result(None, None, GAME_ACTIVE)
        if self.admin_handler.is_active:
            # A2: close the admin session (lock and span) before the start; its serial joins no roster.
            self.admin_handler.abandon_session("operator_pad_start")
        controllers = await self.settings_ops.force_start_roster(None, span)
        if self._lifecycle_live():
            # A start won the race during the roster lookup (ready auto start, another caller).
            return Result(None, None, GAME_ACTIVE)
        if self._generation_stale(expected):
            # A game started and ended during the roster lookup: the lobby is back but not the one held.
            return Result(None, None, STALE)
        self.set_menu_state(menu_pb2.MenuState.GAME_STARTING)
        await self.event_publisher.publish(
            "game_requested",
            {
                "game_name": self.current_selection.name,
                "source": "operator_pad",
                "controllers": json.dumps(controllers),
            },
        )
        await self._start_game(serial="", source="operator_pad", controllers=controllers)
        return Result(None, self.current_selection.name)

    def _generation_stale(self, expected: str) -> bool:
        """UC-015 A5: an ``expected_generation`` was sent and is not the current one (empty = no check)."""
        if expected == "":
            return False
        try:
            return int(expected) != self.lifecycle_generation
        except ValueError:
            return True

    async def _reject_web_start_mode(self, requested_mode: str, span) -> str:
        """Reject a web start_game naming an unknown or unimplemented mode (UC-011 BR-002).

        Publishes ``game_start_rejected`` and returns the error; the selection
        stays and no game starts.
        """
        error = f"Unknown or unimplemented game mode: {requested_mode}"
        logger.warning(f"Web start_game rejected: {error}")
        span.set_attribute("error", error)
        await self.event_publisher.publish(
            "game_start_rejected", {"game_name": requested_mode, "source": "web", "error": error}
        )
        return error

    async def _handle_reset_menu(self) -> None:
        """Handle reset_menu input type.

        UC-010 BR-008 (#2054): reset_menu is the operator's recovery for a lifecycle task that
        never finishes. A live task whose game is not active on the coordinator is cancelled and
        the lobby restored; a live task with an active game is kept.
        """
        if self._lobby_restarting:
            # A lobby restore (this handler's or a lifecycle's post-game one) finishes by itself;
            # cancelling it mid state_manager.reset() would drop controllers not yet re-registered.
            logger.info("reset_menu ignored: a lobby restore is already running")
            return
        task = self._game_lifecycle_task
        if task is not None and not task.done():
            if await self._primary_game_active():
                logger.warning("reset_menu ignored: a game is starting or running on the coordinator")
                return
            if self._game_lifecycle_task is not task or task.done():
                # The lifecycle finished (or a new start replaced it) during the lookup: nothing to recover.
                logger.info("reset_menu ignored: the lifecycle task changed during the game-state lookup")
                return
            logger.warning("reset_menu: cancelling a game lifecycle task with no active game")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await self.event_publisher.publish("game_start_cancelled", {})
            # The restore runs as the lifecycle task, so a start in this window gets the A8
            # lobby-restart refusal instead of interleaving with the lobby restore.
            restore = asyncio.create_task(self._restart_lobby())
            self._game_lifecycle_task = restore
            try:
                await restore
            finally:
                if self._game_lifecycle_task is restore:
                    self._game_lifecycle_task = None
            return
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

    async def _primary_game_active(self) -> bool:
        """Whether the coordinator's primary game is starting or running (UC-010 BR-008).

        Fails closed: an error or an unsuccessful answer counts as active, so reset_menu never
        cancels a live game's stream on a lookup failure.
        """
        from proto import game_coordinator_pb2, game_coordinator_pb2_grpc

        stub = game_coordinator_pb2_grpc.GameCoordinatorServiceStub(self.game_coordinator_channel)
        try:
            response = await stub.GetGameState(game_coordinator_pb2.GetGameStateRequest(), timeout=2.0)
        except Exception as e:
            logger.warning(f"GetGameState failed, treating the game as active: {e}")
            return True
        if not response.success:
            logger.warning(f"GetGameState unsuccessful, treating the game as active: {response.error}")
            return True
        return response.game_info.state in (
            game_coordinator_pb2.GameState.STARTING,
            game_coordinator_pb2.GameState.RUNNING,
        )

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

    async def _start_game(
        self,
        serial: str,
        source: str = "controller",
        controllers: list[str] | None = None,
        game_mode: Games | None = None,
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
        """
        # UC-010 A8 (#2054): a live lifecycle task is a game that is starting or running. Refuse
        # the start; never cancel the live game's stream. A finished task needs no cancel.
        if self._lifecycle_live():
            await self._reject_start_while_active(serial, source, game_mode)
            return False

        # Schedule game lifecycle as background task to avoid blocking button monitor
        self._game_lifecycle_task = asyncio.create_task(
            self._run_game_lifecycle(serial, source, controllers, game_mode=game_mode)
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
        """
        from proto import game_coordinator_pb2, game_coordinator_pb2_grpc

        # Brief delay to let button monitor finish current event dispatch
        await asyncio.sleep(0.1)
        mode = game_mode if game_mode is not None else self.current_selection

        metrics.button_presses_total.labels(button="trigger", action="press").inc()

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

            await self._prepare_game_start()

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
                if self.state == menu_pb2.MenuState.GAME_STARTING:
                    await self._restart_lobby()
                raise

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
            self.state = menu_pb2.MenuState.RUNNING
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
        restore always ends and the idle monitor always starts.
        """

        async def start_button_monitor_and_wait() -> None:
            await self.start_button_monitor()
            await self.controller_events.wait_for_connection()

        # UC-010 A8: a start refused in this window beeps (the players are back in the lobby).
        self._lobby_restarting = True
        try:
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

            # Restart idle monitor with fresh timer
            self.idle_monitor.start()
        finally:
            self._lobby_restarting = False

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
