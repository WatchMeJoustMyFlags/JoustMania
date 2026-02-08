"""OpenTelemetry initialization for PS Move pairing daemon.

Delegates to the shared lib.telemetry module. Service identity
(name, namespace) is configured via environment variables.
"""

from opentelemetry import trace

from lib.telemetry import init_telemetry as _init_telemetry


def init_telemetry() -> trace.Tracer:
    """Initialize OpenTelemetry with OTLP exporter."""
    return _init_telemetry()
