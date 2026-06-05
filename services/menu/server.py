"""
Menu gRPC Server for JoustMania

Manages the game selection menu and lobby experience:
- Game mode selection and cycling
- Controller lobby state (connected/ready)
- LED feedback based on game mode and player state
- Admin mode for in-game configuration
- Real-time event streaming for UI updates

See services/menu/README.md for full documentation.
"""

import asyncio
import contextlib
import logging
import os
import signal

# Configure logging early, before any logging calls
# This must happen before any logging.warning/info/etc to ensure LOG_LEVEL is respected
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

import grpc.aio
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from lib.otel_logging import init_logging
from lib.otel_metrics import init_metrics
from lib.profiling import init_profiling
from lib.system_metrics import start_system_metrics_collector
from lib.types import Games
from proto import menu_pb2, menu_pb2_grpc
from services.menu import metrics
from services.menu.servicer import MenuServicer

logger = logging.getLogger(__name__)


async def serve(port=50054):
    """Start the Menu gRPC server."""
    # Initialize OTEL push metrics
    init_metrics()
    init_logging()
    init_profiling()
    logger.info("OTEL push metrics initialized for menu service")

    # Start system metrics collection
    background_tasks = []
    metrics_task = start_system_metrics_collector(
        cpu_counter=metrics.process_cpu_seconds_total,
        memory_gauge=metrics.process_resident_memory_bytes,
        threads_gauge=metrics.process_threads,
    )
    background_tasks.append(metrics_task)

    # Create server with tracing interceptors for distributed trace propagation
    from lib.grpc_tracing import get_server_interceptors
    from lib.grpc_utils import get_server_options

    server = grpc.aio.server(
        options=get_server_options(),
        interceptors=get_server_interceptors(),
    )

    # Add servicer
    menu_servicer = MenuServicer()
    menu_pb2_grpc.add_MenuServiceServicer_to_server(menu_servicer, server)

    # Add health checking service
    health_servicer = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)

    # Mark the Menu service as SERVING
    await health_servicer.set("menu.MenuService", health_pb2.HealthCheckResponse.SERVING)
    await health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)

    # Bind to port
    server.add_insecure_port(f"[::]:{port}")

    # Start server
    logger.info(f"Starting Menu gRPC server on port {port}")
    await server.start()

    # Start controller button monitoring
    await menu_servicer.start_button_monitor()

    # Start game event monitoring (stops button monitor during games)
    await menu_servicer.start_game_event_monitor()

    # Auto-start menu (so controllers light up immediately)
    # Read from flagd user domain (initialized by MenuServicer.__init__)
    auto_start = menu_servicer.user_prefs_client.get_boolean_value("menu_auto_start", True)
    if auto_start:
        menu_servicer.state = menu_pb2.MenuState.RUNNING
        menu_servicer.current_selection = Games.JoustFFA
        logger.info("Menu auto-started (menu_auto_start flag=true)")

    # Use asyncio.Event for signal-driven shutdown so both SIGTERM (Docker stop)
    # and SIGINT (Ctrl-C / KeyboardInterrupt) trigger graceful shutdown.
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    await shutdown_event.wait()

    logger.info("Shutting down Menu server...")

    # Cancel background tasks
    for task in background_tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    logger.info("Background tasks cancelled")

    await menu_servicer.stop_button_monitor()
    await menu_servicer.stop_game_event_monitor()
    await menu_servicer.shutdown()
    await server.stop(grace=5)


if __name__ == "__main__":
    asyncio.run(serve())
