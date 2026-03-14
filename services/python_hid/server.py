"""
Python HID gRPC Server entry point.

Standalone gRPC service implementing the ControllerIOService from psmove_hid.proto.
Provides the same interface as rust-hid for apples-to-apples canary comparison.
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
from lib.profiling import init_profiling
from lib.system_metrics import start_system_metrics_collector
from proto import psmove_hid_pb2_grpc
from services.python_hid import metrics
from services.python_hid.device_manager import DeviceManager
from services.python_hid.servicer import ControllerIOServicer

logger = logging.getLogger(__name__)


async def serve(port=50059):
    """Start the Python HID async gRPC server."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Initialize OTEL
    init_metrics()
    init_logging()
    init_profiling()

    # Start system metrics collection
    start_system_metrics_collector(
        cpu_counter=metrics.process_cpu_seconds_total,
        memory_gauge=metrics.process_resident_memory_bytes,
        threads_gauge=metrics.process_threads,
    )

    # Start device manager thread
    device_manager = DeviceManager()
    device_manager.start()

    # Create gRPC server
    from lib.grpc_tracing import get_server_interceptors
    from lib.grpc_utils import get_server_options

    server = grpc.aio.server(
        options=get_server_options(),
        interceptors=get_server_interceptors(),
    )

    # Register servicer
    servicer = ControllerIOServicer(device_manager)
    psmove_hid_pb2_grpc.add_ControllerIOServiceServicer_to_server(servicer, server)

    # Add health checking
    health_servicer = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    await health_servicer.set("psmove_hid.ControllerIOService", health_pb2.HealthCheckResponse.SERVING)
    await health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)

    # Start server
    server.add_insecure_port(f"[::]:{port}")
    logger.info(f"Starting Python HID gRPC server on port {port}")
    await server.start()
    logger.info(f"Python HID server listening on port {port}")

    # Signal-driven shutdown
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    await shutdown_event.wait()

    logger.info("Shutting down Python HID server...")
    device_manager.stop()
    await server.stop(grace=5)


if __name__ == "__main__":
    try:
        import uvloop

        uvloop.install()
        logger.info("uvloop installed for improved asyncio performance")
    except ImportError:
        pass

    asyncio.run(serve())
