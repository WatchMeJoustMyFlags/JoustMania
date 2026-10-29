"""Wiring of the menu service: channels, flag clients, utilities, state manager and handlers (#2380).

`build_menu(servicer)` is the construction code that used to fill `MenuServicer.__init__`. It sets the
collaborators on the servicer it is given (handlers keep `callbacks=servicer`) and has no logic of its own.
The tracer keeps the name `services.menu.servicer`, so the instrumentation scope does not change.
"""

import os

from lib.feature_flags import get_flag_client, init_flag_domain
from lib.flag_service_writer import FlagServiceWriter
from lib.telemetry import get_tracer
from services.menu import metrics
from services.menu.controller_events import ControllerEventLoop
from services.menu.event_publisher import EventPublisher
from services.menu.handlers import AdminModeHandler, ConnectedHandler, IdleHandler, ReadyHandler
from services.menu.idle_monitor import IdleMonitor, read_sentinel_animation
from services.menu.settings_ops import SettingsOps
from services.menu.state_manager import StateManager
from services.menu.utils import AudioHelper, LedController
from services.menu.utils.settings import DEFAULT_VOICE_ACTOR

tracer = get_tracer("services.menu.servicer")


def build_menu(servicer) -> None:
    """Create the channels, clients, utilities, state manager, handlers and monitors on `servicer`."""
    # Persistent gRPC channels
    from lib.grpc_utils import create_channel

    controller_host = os.getenv("CONTROLLER_MANAGER_HOST", "controller-manager")
    controller_port = os.getenv("CONTROLLER_MANAGER_PORT", "50052")
    game_coordinator_host = os.getenv("GAME_COORDINATOR_HOST", "game-coordinator")
    game_coordinator_port = os.getenv("GAME_COORDINATOR_PORT", "50053")
    audio_host = os.getenv("AUDIO_HOST", "audio")
    audio_port = os.getenv("AUDIO_PORT", "50056")

    servicer.controller_channel = create_channel(f"{controller_host}:{controller_port}")
    servicer.game_coordinator_channel = create_channel(f"{game_coordinator_host}:{game_coordinator_port}")
    servicer.audio_channel = create_channel(f"{audio_host}:{audio_port}")

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
    servicer.game_settings_writer = FlagServiceWriter("game")
    servicer.user_prefs_writer = FlagServiceWriter("user")
    # Agent policy writer for the on-device kill-switch (#819). The human
    # owns agent policy per docs/OWNERSHIP_MODEL.md §2/§6.2. The agent set is
    # served by the flag service (#1412): the kill switch is a persisted admin
    # setting (PUT /agent/interventions_allowed {"persist": true}, FR-019, D10).
    servicer.agent_settings_writer = FlagServiceWriter("agent")
    # The operator pad's kill switch and write gate (#2050, #2093, UC-015 BR-001): agent/enabled and
    # agent/interventions_enabled have no persist in the registry (a persisted write is 400) and
    # must not survive a restart (UC-045 step 7).
    servicer.agent_enabled_writer = FlagServiceWriter("agent", persist=False)

    # OpenFeature clients for reading settings
    servicer.game_settings_client = get_flag_client("game")
    servicer.user_prefs_client = get_flag_client("user")
    servicer.system_client = get_flag_client("system")
    # Client for resolving effective (agent-override-aware) values for the
    # admin display (#818) and the kill-switch level (#819).
    servicer.agent_settings_client = get_flag_client("agent")

    # Utility classes
    servicer.led = LedController(servicer.controller_channel)
    servicer.audio = AudioHelper(servicer.audio_channel, user_prefs_client=servicer.user_prefs_client)

    # Event publisher for streaming menu events
    servicer.event_publisher = EventPublisher(tracer, metrics)

    # State manager and handlers
    servicer.state_manager = StateManager(
        led=servicer.led,
        audio=servicer.audio,
        game_settings_writer=servicer.game_settings_writer,
        user_prefs_writer=servicer.user_prefs_writer,
        agent_settings_writer=servicer.agent_settings_writer,
        agent_settings_client=servicer.agent_settings_client,
        publish_event=servicer.event_publisher.publish,
        select_game_mode=servicer.select_game_mode,
    )

    # Register handlers with state manager
    servicer.connected_handler = ConnectedHandler()
    servicer.ready_handler = ReadyHandler(start_game_callback=servicer._start_game)
    servicer.state_manager.register_handler(servicer.connected_handler)
    servicer.state_manager.register_handler(servicer.ready_handler)

    # Voice profile (flag menu_voice: gen_female or gen_male)
    servicer.voice_actor = DEFAULT_VOICE_ACTOR

    # Settings core shared by admin mode and the operator pad commands (#2050, UC-015).
    servicer.settings_ops = SettingsOps(
        get_state_manager=lambda: servicer.state_manager,
        controller_channel=servicer.controller_channel,
        metrics=metrics,
        callbacks=servicer,
        agent_enabled_writer=servicer.agent_enabled_writer,
    )

    # Admin mode handler
    servicer.admin_handler = AdminModeHandler(
        controller_channel=servicer.controller_channel,
        tracer=tracer,
        callbacks=servicer,
        metrics=metrics,
        settings_ops=servicer.settings_ops,
    )
    servicer.state_manager.register_handler(servicer.admin_handler)

    # Idle handler and monitor
    servicer.idle_handler = IdleHandler()
    servicer.state_manager.register_handler(servicer.idle_handler)

    servicer.idle_monitor = IdleMonitor(
        state_manager=servicer.state_manager,
        get_idle_enabled=lambda: servicer.system_client.get_boolean_value("idle.enabled", True),
        get_idle_timeout=lambda: servicer.system_client.get_integer_value("idle.timeout_minutes", 15),
        get_sentinel_count=lambda: servicer.system_client.get_integer_value("sentinel.count", 2),
        get_rotation_minutes=lambda: servicer.system_client.get_integer_value("sentinel.rotation_minutes", 5),
        get_sentinel_animation=lambda: read_sentinel_animation(servicer.system_client),
    )

    # Wire idle handler's wake callback to idle monitor
    servicer.idle_handler.set_wake_callback(servicer.idle_monitor.wake_all)

    # Controller event loop (handles button and connection events)
    servicer.controller_events = ControllerEventLoop(
        controller_channel=servicer.controller_channel,
        led=servicer.led,
        callbacks=servicer,
        metrics=metrics,
    )
