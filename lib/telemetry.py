"""
Shared OpenTelemetry initialization for JoustMania services.

Provides consistent telemetry setup across all services with:
- OTLP trace exporting
- Standard resource attributes
- Span attribute constants
- Lazy initialization for faster service startup

Usage:
    from lib.telemetry import get_tracer, SpanAttr

    # Lazy initialization - defers setup until first span is created
    tracer = get_tracer(__name__)

    # Use span attribute constants
    span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)

    # Legacy usage (still works, but prefer get_tracer for lazy init)
    tracer = init_telemetry()
"""

import logging
import os
import threading

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import get_global_textmap
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from lib.otel_resource import get_otel_resource

logger = logging.getLogger(__name__)

# Lazy initialization state
_initialized = False
_init_lock = threading.Lock()
_test_mode = False


def disable_telemetry_for_tests() -> None:
    """
    Disable OpenTelemetry tracing for unit tests.

    Call this once in conftest.py to prevent OTLP connection attempts
    during tests. This is cleaner than patching sys.modules.

    Example:
        # conftest.py
        from lib.telemetry import disable_telemetry_for_tests
        disable_telemetry_for_tests()
    """
    global _initialized, _test_mode
    _test_mode = True
    _initialized = True  # Prevent real initialization

    # Set environment variables as backup for any code that checks them
    os.environ["OTEL_SDK_DISABLED"] = "true"
    os.environ["OTEL_TRACES_EXPORTER"] = "none"
    os.environ["OTEL_METRICS_EXPORTER"] = "none"


def is_test_mode() -> bool:
    """Check if telemetry is in test mode (disabled for tests)."""
    return _test_mode


class SpanAttr:
    """Constants for OpenTelemetry span attribute names.

    Using constants prevents typos and enables IDE autocomplete.
    """

    # Controller attributes
    CONTROLLER_SERIAL = "controller.serial"

    # Admin mode attributes
    ADMIN_OPTION = "admin.option"

    # Validation attributes
    VALIDATION_RESULT = "validation.result"
    VALIDATION_REASON = "validation.reason"


def _do_init() -> None:
    """Perform actual OpenTelemetry initialization from env vars (internal)."""
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    service_name = os.getenv("OTEL_SERVICE_NAME", "unknown-service")

    resource = get_otel_resource()

    from lib.feature_flags import init_flag_domain
    from lib.flagd_sampler import FlagdSampler
    from lib.flagd_span_processor import FlagdSpanEnrichmentProcessor

    # Ensure the performance flag domain is initialized before creating
    # flag-driven components so they don't evaluate against a no-op provider.
    init_flag_domain("performance")

    provider = TracerProvider(resource=resource, sampler=FlagdSampler())
    otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    provider.add_span_processor(FlagdSpanEnrichmentProcessor())

    # Trace-profile correlation: link Pyroscope CPU profiles to trace spans
    from lib.profiling import _is_profiling_enabled

    if _is_profiling_enabled():
        try:
            from pyroscope.otel import PyroscopeSpanProcessor

            provider.add_span_processor(PyroscopeSpanProcessor())
            logger.info("Pyroscope span processor enabled for trace-profile correlation")
        except ImportError:
            pass  # pyroscope-io not installed

    trace.set_tracer_provider(provider)

    logger.info(f"OpenTelemetry initialized: {service_name} -> {otlp_endpoint}")


def _ensure_initialized() -> None:
    """Ensure OpenTelemetry is initialized (lazy, thread-safe, idempotent)."""
    global _initialized
    if _initialized:
        return

    with _init_lock:
        # Double-check after acquiring lock
        if _initialized:
            return

        _do_init()
        _initialized = True


def get_tracer(name: str | None = None) -> trace.Tracer:
    """
    Get a tracer with lazy initialization.

    This is the preferred way to get a tracer. Initialization is deferred
    until the first call, avoiding startup delays from OTLP connection setup.

    Args:
        name: Tracer name (typically __name__). Defaults to service name.

    Returns:
        Configured tracer instance for creating spans.

    Example:
        tracer = get_tracer(__name__)
        with tracer.start_as_current_span("my-operation"):
            ...
    """
    _ensure_initialized()
    return trace.get_tracer(name or os.getenv("OTEL_SERVICE_NAME", "unknown-service"))


def init_telemetry() -> trace.Tracer:
    """
    Initialize OpenTelemetry with OTLP exporter (legacy API).

    Note: Prefer get_tracer() for lazy initialization. This function is
    kept for backward compatibility. Service identity is configured via
    environment variables set in docker-compose.yml.

    Returns:
        Configured tracer instance for creating spans.

    Environment Variables:
        OTEL_SERVICE_NAME: Service name (required in docker-compose.yml)
        OTEL_SERVICE_NAMESPACE: Service namespace (default: "joustmania")
        OTEL_EXPORTER_OTLP_ENDPOINT: OTLP collector endpoint (default: http://localhost:4317)
    """
    _ensure_initialized()
    return trace.get_tracer(os.getenv("OTEL_SERVICE_NAME", "unknown-service"))


def inject_trace_context(span: trace.Span | None = None) -> tuple[str, str]:
    """
    Get trace context as W3C traceparent/tracestate strings.

    Extracts trace context and serializes it for propagation across
    service boundaries (e.g., via gRPC messages).

    Args:
        span: Optional span to get context from. If None, uses current active span.
              Use this to create child spans under a specific parent (e.g., player
              lifecycle span) rather than the current active span.

    Returns:
        Tuple of (trace_parent, trace_state) strings.
        Returns empty strings if no active span context.
    """
    carrier: dict[str, str] = {}
    propagator = get_global_textmap()

    if span is not None:
        # Create a context with the specified span and inject from it
        ctx = trace.set_span_in_context(span)
        propagator.inject(carrier, context=ctx)
    else:
        # Use current active context
        propagator.inject(carrier)

    return carrier.get("traceparent", ""), carrier.get("tracestate", "")


def extract_trace_context(trace_parent: str, trace_state: str) -> otel_context.Context | None:
    """
    Restore trace context from W3C traceparent/tracestate strings.

    Deserializes trace context received from another service to allow
    creating child spans that are linked to the original trace.

    Args:
        trace_parent: W3C traceparent header value
        trace_state: W3C tracestate header value

    Returns:
        Context object that can be passed to tracer.start_span(context=...).
        Returns None if trace_parent is empty or invalid.
    """
    if not trace_parent:
        return None

    carrier = {"traceparent": trace_parent}
    if trace_state:
        carrier["tracestate"] = trace_state

    propagator = get_global_textmap()
    return propagator.extract(carrier)
