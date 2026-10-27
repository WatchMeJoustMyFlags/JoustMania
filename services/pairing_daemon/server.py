#!/usr/bin/env python3
"""
PS Move Controller Pairing Daemon.

Polls for USB-connected PS Move controllers and pairs them automatically
using hidapi for direct HID feature report access. Monitors Bluetooth-connected
controllers for signal strength and connection status.
Provides OTEL push metrics and OpenTelemetry tracing for observability.

Runs as a Docker container alongside other JoustMania services.

LED Feedback: while a controller is on USB it shows the pairing state: slow white
pulse while waiting or pairing, three green flashes when paired, steady red when
pairing failed (UC-001 BR-010). Progress is also in the logs and metrics.

Environment:
  METRICS_PORT  - port for the health check and control API (default: 8002)
  CONTROL_BIND  - comma-separated addresses that port binds to (default: 127.0.0.1).
                  Full stack: 127.0.0.1 plus the docker0 gateway (172.17.0.1), so
                  the connect-proxy reaches it via host.docker.internal but the
                  LAN does not. An address that cannot be bound yet (docker0
                  not up) is retried in the background with backoff; /healthz
                  reports the requested and bound addresses (#1450).
  OTEL_EXPORTER_OTLP_ENDPOINT - OTLP collector endpoint (default: http://localhost:4317)
  FLAGD_HOST    - flagd host for feature flags (default: flagd)
  FLAGD_PORT    - flagd sync port (default: 8015)

Feature Flags (system domain):
  pairing.poll_interval        - seconds between USB polls (default: 10)
  pairing.bt_monitor_interval  - seconds between Bluetooth monitoring (default: 5)

Endpoints:
  GET /healthz  - Health check (200 if healthy, 503 if unhealthy)
  POST /controllers/{address}/unpair - Unpair one controller on every adapter
      (UC-005; operator-only, no authentication, reached via the connect-proxy)
"""

import asyncio
import json
import logging
import os
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread

from lib.log_level import refresh_log_level, register_floor
from lib.otel_logging import init_logging
from lib.otel_metrics import init_metrics
from lib.system_metrics import start_system_metrics_collector_thread
from psmove_pairing import PairingDaemon, init_telemetry, metrics
from psmove_pairing.config import METRICS_PORT, init_performance_flags, is_debug
from psmove_pairing.control_api import handle_post

# Global daemon reference for health checks
_daemon: PairingDaemon | None = None

# Logging setup
logging.basicConfig(
    # Bootstrap level only: flagd is not read yet; the `debug` flag takes over (#1788)
    level=logging.INFO,
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

    def do_POST(self):
        """Handle POST requests (operator control API, UC-005)."""
        if _daemon is None:
            self._send_json(503, {"error": "daemon not started"})
            return
        result = handle_post(self.path, _daemon.usb_pairing, _daemon.loop)
        if result is None:
            self.send_response(404)
            self.end_headers()
            return
        self._send_json(*result)

    def _send_json(self, status: int, body: dict) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def _handle_healthz(self):
        """Handle health check request."""
        global _daemon

        if _daemon is None:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            body = {"healthy": False, "error": "daemon not started", "control_bind": _control_bind.snapshot()}
            self.wfile.write(json.dumps(body).encode())
            return

        status = {**_daemon.get_health_status(), "control_bind": _control_bind.snapshot()}
        if status["healthy"]:
            self.send_response(200)
        else:
            self.send_response(503)

        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(status).encode())

    def log_message(self, format, *args):
        """Suppress default request logging."""


def bind_addresses(value: str | None) -> list[str]:
    """Addresses from CONTROL_BIND (comma-separated); default loopback only."""
    addresses = [a.strip() for a in (value or "").split(",") if a.strip()]
    return addresses or ["127.0.0.1"]


# Backoff for an address that cannot be bound yet (#1450): 1 s, 2 s, 4 s ... capped.
BIND_RETRY_INITIAL_SECONDS = 1.0
BIND_RETRY_MAX_SECONDS = 30.0

ServerFactory = Callable[[tuple[str, int], type[BaseHTTPRequestHandler]], ThreadingHTTPServer]


class ControlBindStatus:
    """The CONTROL_BIND addresses requested and actually bound, for /healthz (#1450).

    A requested address that is not bound means the connect-proxy may get 502 on
    every unpair (docker0 gateway) while /healthz on loopback stays green.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._requested: list[str] = []
        self._bound: set[str] = set()

    def set_requested(self, addresses: list[str]) -> None:
        with self._lock:
            self._requested = list(addresses)
            self._bound = set()
            self._publish()

    def mark_bound(self, address: str) -> None:
        with self._lock:
            self._bound.add(address)
            self._publish()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "requested": list(self._requested),
                "bound": [a for a in self._requested if a in self._bound],
            }

    def _publish(self) -> None:
        unbound = sum(1 for a in self._requested if a not in self._bound)
        metrics.pairing_control_bind_unbound_addresses.set(unbound)


_control_bind = ControlBindStatus()


def _serve(server: ThreadingHTTPServer) -> None:
    Thread(target=server.serve_forever, daemon=True).start()


def _retry_bind(
    address: str,
    port: int,
    server_factory: ServerFactory,
    status: ControlBindStatus,
    stop: Event,
    on_bound: Callable[[ThreadingHTTPServer], None],
    initial_delay: float,
    max_delay: float,
) -> None:
    """Keep trying to bind one address, doubling the wait up to max_delay."""
    delay = initial_delay
    attempt = 1
    while not stop.wait(delay):
        attempt += 1
        try:
            server = server_factory((address, port), HealthHandler)
        except OSError as e:
            logger.warning(
                f"Bind retry {attempt} for {address}:{port} failed: {e}; next in {min(delay * 2, max_delay):.0f}s"
            )
            delay = min(delay * 2, max_delay)
            continue
        _serve(server)
        status.mark_bound(address)
        on_bound(server)
        logger.info(f"HTTP server (healthz, control API) listening on {address}:{port} (after {attempt} attempts)")
        return


def start_health_server(
    port: int,
    addresses: list[str],
    *,
    status: ControlBindStatus | None = None,
    server_factory: ServerFactory = ThreadingHTTPServer,
    stop: Event | None = None,
    initial_delay: float = BIND_RETRY_INITIAL_SECONDS,
    max_delay: float = BIND_RETRY_MAX_SECONDS,
) -> list[ThreadingHTTPServer]:
    """Serve /healthz and the control API on each address (UC-005 BR-003).

    The port is not bound on every interface: on the host network that would
    make the unpair route reachable from the LAN. An address that cannot be
    bound (e.g. docker0 not up yet) is logged and retried in a background
    thread with backoff until it binds or ``stop`` is set (#1450). The returned
    list holds the servers bound so far and grows when a retry succeeds.
    """
    status = status if status is not None else _control_bind
    stop = stop if stop is not None else Event()
    status.set_requested(addresses)
    servers: list[ThreadingHTTPServer] = []
    for address in addresses:
        try:
            server = server_factory((address, port), HealthHandler)
        except OSError as e:
            logger.error(f"Cannot bind HTTP server to {address}:{port}: {e}; retrying in the background")
            Thread(
                target=_retry_bind,
                args=(address, port, server_factory, status, stop, servers.append, initial_delay, max_delay),
                name=f"control-bind-retry-{address}",
                daemon=True,
            ).start()
            continue
        _serve(server)
        status.mark_bound(address)
        logger.info(f"HTTP server (healthz, control API) listening on {address}:{port}")
        servers.append(server)
    return servers


def main() -> None:
    """Entry point."""
    global _daemon

    # Initialize OTEL push metrics
    init_metrics()
    init_logging()
    logger.info("OTEL push metrics initialized for pairing daemon")

    # The `debug` flag lowers the log level to DEBUG at runtime (#1788)
    register_floor(lambda: logging.DEBUG if is_debug() else None)

    # Initialize feature flags for runtime-tunable intervals
    init_performance_flags()
    refresh_log_level()

    # Start process-level system metrics collection (CPU, memory, threads)
    start_system_metrics_collector_thread(
        cpu_counter=metrics.process_cpu_seconds_total,
        memory_gauge=metrics.process_resident_memory_bytes,
        threads_gauge=metrics.process_threads,
    )

    # Start health check HTTP server
    start_health_server(METRICS_PORT, bind_addresses(os.getenv("CONTROL_BIND")))

    # Initialize OpenTelemetry tracing
    tracer = init_telemetry()

    # Create and run daemon
    _daemon = PairingDaemon(tracer)
    asyncio.run(_daemon.run())


if __name__ == "__main__":
    main()
