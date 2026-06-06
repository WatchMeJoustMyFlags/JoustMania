"""
ControllerManager gRPC Server entry point for JoustMania.

This module provides the entry point for the ControllerManager service.
The actual servicer implementation is in servicer.py.
"""

import asyncio
import logging
import os
import signal

import grpc
import grpc.aio
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from lib.otel_logging import init_logging
from lib.otel_metrics import init_metrics
from lib.system_metrics import start_system_metrics_collector
from proto import controller_manager_pb2_grpc
from services.controller_manager import metrics
from services.controller_manager.servicer import ControllerManagerServicer

logger = logging.getLogger(__name__)


def _find_mock_backend(backend):
    """Find MockAdapter through MultiplexerBackend."""
    if hasattr(backend, "adapters"):
        for adapter in backend.adapters:
            if adapter.adapter_type == "mock":
                return adapter
    return None


async def serve(port=50052):
    """Start the ControllerManager async gRPC server."""
    # Configure logging with environment variable support
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Initialize flagd for dynamic streaming frequency and tuning flags
    from lib.feature_flags import init_flag_domain, wait_for_provider_ready

    # system domain: game_loop.update_frequency_hz (streaming frequency override)
    init_flag_domain("system")
    # controller domain: backend, bluetooth_backend, chaos_fault_type, poll_drop_threshold
    init_flag_domain("controller")
    # rollout domain: strategy, target_backend, current_controller_count
    # (consumed by RolloutRouter during per-cycle adapter routing).
    init_flag_domain("rollout")
    # Wait for all domains to be ready before backend creation: create_backend()
    # evaluates the controller domain, the frequency listener evaluates the
    # system domain, and the multiplexer's RolloutRouter evaluates the rollout
    # domain on the first routing cycle.
    # Deadline must be shorter than the Docker HEALTHCHECK window (start_period + retries * interval).
    await wait_for_provider_ready("system", deadline_seconds=5.0)
    await wait_for_provider_ready("controller", deadline_seconds=5.0)
    await wait_for_provider_ready("rollout", deadline_seconds=5.0)

    # Initialize OTEL push metrics
    # Export interval read from flagd with per-service targeting (Issue #479)
    # Controller-manager gets 100ms (realtime), other services get 1000ms (normal)
    init_metrics()
    init_logging()

    from services.controller_manager.servicer import init_frequency_listener

    init_frequency_listener()

    # Enable Bluetooth adapters before backend creation (rfkill unblock)
    try:
        from services.controller_manager.backend_factory import initialize_bt_adapters

        await initialize_bt_adapters()
    except Exception:
        logger.warning("Failed to initialize Bluetooth adapters (may not be available)", exc_info=True)

    # Create servicer BEFORE initializing other flag domains.
    # The FlagdProvider in-process resolver loses flags from the controller domain
    # when a second provider (game) is initialized — causing FLAG_NOT_FOUND
    # for the backend flag.
    controller_servicer = ControllerManagerServicer()

    # Initialize flagd game domain for winner_rainbow_duration_ms (Issue #464)
    from services.controller_manager.feedback_manager import init_game_settings_listener

    init_game_settings_listener()

    # Start prometheus_client HTTP server for direct pull scraping (pipeline comparison)
    from prometheus_client import start_http_server

    metrics_port = int(os.getenv("PROMETHEUS_METRICS_PORT", "8000"))
    start_http_server(metrics_port)
    logger.info(f"Prometheus pull metrics available at http://0.0.0.0:{metrics_port}/metrics")

    # Start system metrics collection (Phase 61: extracted to lib/system_metrics.py)
    start_system_metrics_collector(
        cpu_counter=metrics.process_cpu_seconds_total,
        memory_gauge=metrics.process_resident_memory_bytes,
        threads_gauge=metrics.process_threads,
    )

    # Create async server with keepalive options and tracing interceptors
    # Without keepalive options, server rejects client pings as "too many pings"
    from lib.grpc_tracing import get_server_interceptors
    from lib.grpc_utils import get_server_options

    server = grpc.aio.server(
        options=get_server_options(),
        interceptors=get_server_interceptors(),
    )
    controller_manager_pb2_grpc.add_ControllerManagerServiceServicer_to_server(controller_servicer, server)

    # Start discovery loop immediately (don't defer to first stream connection)
    # This ensures controllers are discovered before any clients connect
    controller_servicer.discovery_loop.start()
    controller_servicer._discovery_started = True

    # Wait for backend initialization before accepting connections
    init_success = await controller_servicer.discovery_loop.wait_initialized(timeout_seconds=10.0)
    if init_success:
        logger.info("Discovery loop started and backend initialized")
    else:
        logger.warning("Discovery loop started but backend initialization may have failed")

    # Add health checking service
    health_servicer = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)

    # Mark the ControllerManager service as SERVING
    await health_servicer.set("controller_manager.ControllerManagerService", health_pb2.HealthCheckResponse.SERVING)
    await health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)  # Overall health

    # Bind to port
    server.add_insecure_port(f"[::]:{port}")

    # Start server
    logger.info(f"Starting ControllerManager async gRPC server on port {port}")
    await server.start()

    logger.info(f"ControllerManager server listening on port {port}")

    # If using mock backend, start MockControllerService on port 50062
    mock_server = None
    mock_backend = _find_mock_backend(controller_servicer.backend)
    if mock_backend is not None:
        from proto import controller_manager_mock_pb2_grpc
        from services.controller_manager.mock_control_service import MockControllerService

        mock_server = grpc.aio.server(options=get_server_options())
        mock_servicer = MockControllerService(mock_backend)
        controller_manager_mock_pb2_grpc.add_MockControllerServiceServicer_to_server(mock_servicer, mock_server)

        mock_port = 50062
        mock_server.add_insecure_port(f"[::]:{mock_port}")
        await mock_server.start()
        logger.info(f"MockControllerService listening on port {mock_port}")

    # Use asyncio.Event for signal-driven shutdown so both SIGTERM (Docker stop)
    # and SIGINT (Ctrl-C / KeyboardInterrupt) trigger graceful shutdown.
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    await shutdown_event.wait()

    logger.info("Shutting down ControllerManager server...")
    await controller_servicer.shutdown()
    await server.stop(grace=5)

    # Stop mock server if running
    if mock_server:
        await mock_server.stop(grace=5)


if __name__ == "__main__":
    # Performance optimization: Use uvloop for faster event loop on Linux
    # uvloop provides 2-4x faster asyncio performance by using libuv
    try:
        import uvloop

        uvloop.install()
        logger.info("uvloop installed for improved asyncio performance")
    except ImportError:
        # uvloop not available (e.g., macOS/Windows development)
        pass

    asyncio.run(serve())
