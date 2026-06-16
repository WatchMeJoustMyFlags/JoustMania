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

# Fail-open default for the BSP flush delay (#828). The OpenTelemetry Python SDK
# default is 5000ms; we keep that as the safe fallback so telemetry init behaves
# exactly as before whenever the flagd read below is unavailable.
_DEFAULT_BSP_SCHEDULE_DELAY_MS = 5000

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


def _get_bsp_schedule_delay_ms(service_name: str) -> int:
    """Read the BSP flush delay from flagd, per-service, fail-open (#828).

    The agent observes ``controller.bluetooth_health`` spans 8-16s late because
    the BatchSpanProcessor's default flush (~5s) compounds with the collector
    traces batch. This makes the flush delay a flagd flag with PER-SERVICE
    TARGETING so controller-manager can flush faster (lower rollout-health
    observe latency) while other services keep the conservative default.

    READ-ONCE-AT-INIT: the SDK fixes schedule_delay at BatchSpanProcessor
    construction, so this is read exactly once during telemetry init and never
    re-tuned live (mirrors lib/otel_metrics._get_export_interval_ms).

    FAIL-OPEN: telemetry init runs very early in service startup, before flagd
    is guaranteed reachable. This MUST NOT break or block telemetry — on ANY
    error (provider not ready, missing flag, evaluation error, non-numeric
    value) it returns the safe SDK-equivalent default. The flagd client itself
    is fail-open with a bounded internal deadline, so an unreachable flagd
    resolves to the supplied default rather than hanging.

    The ``feature_flags`` import is done lazily INSIDE this function on purpose:
    ``lib.feature_flags`` -> ``lib.flag_eval_visibility`` -> ``lib.otel_metrics``,
    so a module-level import here would risk an import cycle through the
    telemetry/metrics shared libs. Mirrors otel_metrics._get_export_interval_ms.
    """
    try:
        from openfeature.evaluation_context import EvaluationContext

        from lib.feature_flags import get_flag_client, init_flag_domain

        init_flag_domain("observability")
        client = get_flag_client("observability")
        delay = client.get_integer_value(
            "otel_bsp_schedule_delay_millis",
            _DEFAULT_BSP_SCHEDULE_DELAY_MS,
            EvaluationContext(attributes={"service": service_name}),
        )
        logger.info(f"otel_bsp_schedule_delay_millis from flagd: {delay}ms (service={service_name})")
        return delay
    except Exception as e:
        logger.warning(
            f"Failed to read otel_bsp_schedule_delay_millis from flagd, "
            f"using default {_DEFAULT_BSP_SCHEDULE_DELAY_MS}ms: {e}"
        )
        return _DEFAULT_BSP_SCHEDULE_DELAY_MS


def _do_init() -> None:
    """Perform actual OpenTelemetry initialization from env vars (internal)."""
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    service_name = os.getenv("OTEL_SERVICE_NAME", "unknown-service")

    resource = get_otel_resource()

    schedule_delay_ms = _get_bsp_schedule_delay_ms(service_name)

    provider = TracerProvider(resource=resource)
    otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(otlp_exporter, schedule_delay_millis=schedule_delay_ms))

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
