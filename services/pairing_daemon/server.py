#!/usr/bin/env python3
"""
PS Move Controller Pairing Daemon.

Polls for USB-connected PS Move controllers and pairs them automatically
using hidapi for direct HID feature report access. Monitors Bluetooth-connected
controllers for signal strength and connection status.
Provides OTEL push metrics and OpenTelemetry tracing for observability.

Runs as a Docker container alongside other JoustMania services.

LED Feedback:
  - Yellow solid: Pairing in progress
  - White flash 3x: Success - unplug and press PS button
  - Red flash 3x: Error

Environment:
  DEBUG         - set to 1 for verbose logging
  HEALTH_PORT   - port for health check endpoint (default: 8002)
  OTEL_EXPORTER_OTLP_ENDPOINT - OTLP collector endpoint (default: http://localhost:4317)
  FLAGD_HOST    - flagd host for feature flags (default: flagd)
  FLAGD_PORT    - flagd sync port (default: 8015)

Feature Flags (system domain):
  pairing.poll_interval        - seconds between USB polls (default: 10)
  pairing.bt_monitor_interval  - seconds between Bluetooth monitoring (default: 5)

Endpoints:
  GET /healthz  - Health check (200 if healthy, 503 if unhealthy)
"""

import asyncio
import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from lib.otel_logging import init_logging
from lib.otel_metrics import init_metrics
from lib.system_metrics import start_system_metrics_collector_thread
from psmove_pairing import PairingDaemon, init_telemetry, metrics
from psmove_pairing.config import DEBUG, METRICS_PORT, init_performance_flags

# Global daemon reference for health checks
_daemon: PairingDaemon | None = None

# Logging setup
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("psmove-pairing")


class HealthHandler(BaseHTTPRequestHandler):
    """HTTP handler for /healthz endpoint."""

    def do_GET(self):
        """Handle GET requests."""
        if self.path in ("/healthz", "/healthz/", "/health", "/health/"):
            self._handle_healthz()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_healthz(self):
        """Handle health check request."""
        global _daemon

        if _daemon is None:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"healthy": False, "error": "daemon not started"}).encode())
            return

        status = _daemon.get_health_status()
        if status["healthy"]:
            self.send_response(200)
        else:
            self.send_response(503)

        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(status).encode())

    def log_message(self, format, *args):
        """Suppress default request logging."""


def start_health_server(port: int) -> None:
    """Start HTTP server with health endpoint."""
    server = HTTPServer(("", port), HealthHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()


def main() -> None:
    """Entry point."""
    global _daemon

    # Initialize OTEL push metrics
    init_metrics()
    init_logging()
    logger.info("OTEL push metrics initialized for pairing daemon")

    # Initialize feature flags for runtime-tunable intervals
    init_performance_flags()

    # Start process-level system metrics collection (CPU, memory, threads)
    start_system_metrics_collector_thread(
        cpu_counter=metrics.process_cpu_seconds_total,
        memory_gauge=metrics.process_resident_memory_bytes,
        threads_gauge=metrics.process_threads,
    )

    # Start health check HTTP server
    logger.info(f"Starting health server on port {METRICS_PORT}")
    start_health_server(METRICS_PORT)

    # Initialize OpenTelemetry tracing
    tracer = init_telemetry()

    # Create and run daemon
    _daemon = PairingDaemon(tracer)
    asyncio.run(_daemon.run())


if __name__ == "__main__":
    main()
