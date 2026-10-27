"""
JoustMania Audio Microservice

Handles audio playback with priority-based mixing and real-time tempo control.
- Sound effects: miniaudio for distroless compatibility
- Background music: MusicPlayer with soxr for real-time tempo control

See services/audio/servicer.py for the AudioServiceServicer implementation.
"""

import asyncio
import logging
import os
import signal

import grpc.aio
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from lib.otel_logging import init_logging
from lib.otel_metrics import init_metrics
from lib.system_metrics import start_system_metrics_collector
from lib.telemetry import get_tracer
from proto import audio_pb2_grpc
from services.audio import metrics
from services.audio.servicer import AudioServiceServicer

logger = logging.getLogger(__name__)


async def serve():
    """Start the Audio gRPC server."""
    # Configure logging
    # Bootstrap level only; the log_level flag takes over once synced (lib/log_level.py, #1788)
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger.info("Starting JoustMania Audio service...")

    # Auto-detect ALSA card and write /etc/asound.conf before opening any audio device
    from services.audio.alsa_config import configure_alsa_device

    alsa_card_setting = os.getenv("ALSA_CARD", "auto")
    configure_alsa_device(alsa_card_setting)

    # Initialize OTEL (get_tracer triggers TracerProvider setup for trace export)
    init_metrics()
    init_logging()
    get_tracer("audio")
    logger.info("OTEL push metrics initialized for audio service")

    # Start system metrics collection
    start_system_metrics_collector(
        cpu_counter=metrics.process_cpu_seconds_total,
        memory_gauge=metrics.process_resident_memory_bytes,
        threads_gauge=metrics.process_threads,
    )

    # Create gRPC server with keepalive options and tracing interceptors
    from lib.grpc_tracing import get_server_interceptors
    from lib.grpc_utils import get_server_options

    server = grpc.aio.server(
        options=get_server_options(),
        interceptors=get_server_interceptors(),
    )
    audio_servicer = AudioServiceServicer()
    audio_pb2_grpc.add_AudioServiceServicer_to_server(audio_servicer, server)

    # Set the event loop for async operations (tempo transitions)
    audio_servicer.audio_manager.set_event_loop(asyncio.get_running_loop())

    # Note: Audio settings are loaded lazily on first PlaySound/PlayMusic call
    # This avoids blocking startup waiting for settings service

    # Add health checking service
    health_servicer = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)

    # Mark the Audio service as SERVING
    await health_servicer.set("audio.AudioService", health_pb2.HealthCheckResponse.SERVING)
    await health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)  # Overall health

    # Bind to port (configurable via AUDIO_PORT env var)
    port = int(os.environ.get("AUDIO_PORT", "50056"))
    server.add_insecure_port(f"[::]:{port}")

    logger.info(f"Audio service listening on port {port}")

    # Start server
    await server.start()

    logger.info("Audio service ready")

    # Use asyncio.Event for signal-driven shutdown so both SIGTERM (Docker stop)
    # and SIGINT (Ctrl-C / KeyboardInterrupt) trigger graceful shutdown.
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    # UC-031 A6: with ALSA_CARD=auto, exit with a dedicated code when a better card appeared so the
    # compose restart policy restarts the container and re-detects it.
    card_change = False

    def _on_card_change() -> None:
        nonlocal card_change
        card_change = True
        shutdown_event.set()

    watcher = None
    if alsa_card_setting == "auto":
        from services.audio.alsa_config import card_watch_busy, get_detected_card, watch_card_changes

        watcher = asyncio.create_task(
            watch_card_changes(
                get_detected_card(),
                _on_card_change,
                is_busy=card_watch_busy(audio_servicer),
            )
        )

    await shutdown_event.wait()

    logger.info("Shutting down Audio service...")
    if watcher is not None:
        watcher.cancel()
    await server.stop(grace=5)
    if card_change:
        from services.audio.alsa_config import CARD_CHANGE_EXIT_CODE

        raise SystemExit(CARD_CHANGE_EXIT_CODE)


if __name__ == "__main__":
    asyncio.run(serve())
