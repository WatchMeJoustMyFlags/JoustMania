"""
Mobile Gateway Server — serves phone UI and bridges WebSocket to gRPC.

Runs on port 8070. Connects to controller-manager MobileControllerService
on port 50063 via gRPC.
"""

import asyncio
import logging
import os
from pathlib import Path

import grpc.aio
from aiohttp import web

from lib.otel_metrics import init_metrics
from lib.profiling import init_profiling
from lib.system_metrics import start_system_metrics_collector
from proto import mobile_controller_pb2_grpc
from services.mobile_gateway import metrics
from services.mobile_gateway.ws_handler import handle_websocket

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


async def create_app() -> web.Application:
    """Create the aiohttp application."""
    app = web.Application()

    # gRPC channel to controller-manager
    controller_host = os.getenv("CONTROLLER_MANAGER_HOST", "controller-manager")
    controller_port = os.getenv("CONTROLLER_MANAGER_MOBILE_PORT", "50063")
    target = f"{controller_host}:{controller_port}"

    channel = grpc.aio.insecure_channel(target)
    app["grpc_channel"] = channel
    app["grpc_stub"] = mobile_controller_pb2_grpc.MobileControllerServiceStub(channel)

    # Routes
    app.router.add_get("/mobile/ws", handle_websocket)
    app.router.add_static("/mobile/", STATIC_DIR, show_index=True)

    # Cleanup on shutdown
    async def cleanup(_app):
        await channel.close()

    app.on_cleanup.append(cleanup)

    return app


async def serve(port: int = 8070):
    """Start the mobile gateway server."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    init_metrics()
    init_profiling()

    # Start system metrics collection
    start_system_metrics_collector(
        cpu_counter=metrics.process_cpu_seconds_total,
        memory_gauge=metrics.process_resident_memory_bytes,
        threads_gauge=metrics.process_threads,
    )

    app = await create_app()

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info(f"Mobile Gateway listening on port {port}")
    logger.info(f"Phone UI: http://0.0.0.0:{port}/mobile/")

    # Run forever
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(serve())
