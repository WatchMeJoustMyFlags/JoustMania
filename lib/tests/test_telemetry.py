"""Tests for lib/telemetry.py"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add lib to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import lib.telemetry as telemetry_module
from lib.telemetry import (
    SpanAttr,
    extract_trace_context,
    get_tracer,
    init_telemetry,
    inject_trace_context,
)


@pytest.fixture(autouse=True)
def _reset_telemetry_state():
    """Reset module-level globals between tests."""
    original_initialized = telemetry_module._initialized
    original_test_mode = telemetry_module._test_mode
    # Set test mode to prevent real OTLP initialization
    telemetry_module._test_mode = True
    telemetry_module._initialized = True
    yield
    telemetry_module._initialized = original_initialized
    telemetry_module._test_mode = original_test_mode


class TestDisableTelemetry:
    """Tests for disable_telemetry_for_tests."""

    def test_sets_test_mode(self):
        telemetry_module._test_mode = False
        telemetry_module._initialized = False

        telemetry_module.disable_telemetry_for_tests()

        assert telemetry_module._test_mode is True
        assert telemetry_module._initialized is True

    def test_sets_env_vars(self):
        telemetry_module.disable_telemetry_for_tests()

        assert os.environ.get("OTEL_SDK_DISABLED") == "true"
        assert os.environ.get("OTEL_TRACES_EXPORTER") == "none"
        assert os.environ.get("OTEL_METRICS_EXPORTER") == "none"

    def test_is_test_mode_true(self):
        telemetry_module.disable_telemetry_for_tests()

        assert telemetry_module.is_test_mode() is True


class TestSpanAttr:
    """Tests for SpanAttr constants."""

    def test_constants_exist(self):
        assert isinstance(SpanAttr.CONTROLLER_SERIAL, str)
        assert isinstance(SpanAttr.ADMIN_OPTION, str)
        assert isinstance(SpanAttr.VALIDATION_RESULT, str)
        assert isinstance(SpanAttr.VALIDATION_REASON, str)

    def test_constants_are_dotted_names(self):
        assert "." in SpanAttr.CONTROLLER_SERIAL
        assert "." in SpanAttr.ADMIN_OPTION


class TestInjectTraceContext:
    """Tests for inject_trace_context."""

    def test_no_active_span(self):
        traceparent, tracestate = inject_trace_context()
        # Without an active span, should return empty strings
        assert isinstance(traceparent, str)
        assert isinstance(tracestate, str)

    def test_returns_tuple_of_two_strings(self):
        result = inject_trace_context()
        assert len(result) == 2
        assert isinstance(result[0], str)
        assert isinstance(result[1], str)


class TestExtractTraceContext:
    """Tests for extract_trace_context."""

    def test_empty_traceparent(self):
        result = extract_trace_context("", "")
        assert result is None

    def test_valid_traceparent(self):
        # W3C traceparent format: version-trace_id-span_id-flags
        traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        result = extract_trace_context(traceparent, "")
        assert result is not None

    def test_with_tracestate(self):
        traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        tracestate = "congo=t61rcWkgMzE"
        result = extract_trace_context(traceparent, tracestate)
        assert result is not None


class TestGetTracer:
    """Tests for get_tracer."""

    def test_returns_tracer(self):
        from opentelemetry import trace

        tracer = get_tracer("test-service")
        assert isinstance(tracer, trace.Tracer)

    def test_with_none_name(self):
        from opentelemetry import trace

        tracer = get_tracer(None)
        assert isinstance(tracer, trace.Tracer)


class TestInitTelemetry:
    """Tests for init_telemetry (legacy API)."""

    def test_returns_tracer(self):
        from opentelemetry import trace

        tracer = init_telemetry()
        assert isinstance(tracer, trace.Tracer)


class TestGetBspScheduleDelay:
    """Tests for _get_bsp_schedule_delay_ms (#828): per-service flagd read, fail-open."""

    def test_fail_open_default_when_flagd_unavailable(self):
        """flagd unreachable / any error → safe SDK-equivalent default, never raises."""
        with patch("lib.feature_flags.init_flag_domain", side_effect=RuntimeError("flagd down")):
            delay = telemetry_module._get_bsp_schedule_delay_ms("controller-manager-service")
        assert delay == telemetry_module._DEFAULT_BSP_SCHEDULE_DELAY_MS

    def test_reads_per_service_value_from_flagd(self):
        """Success path: the resolved per-service flag value is returned."""
        fake_client = MagicMock()
        fake_client.get_integer_value.return_value = 500
        with (
            patch("lib.feature_flags.init_flag_domain"),
            patch("lib.feature_flags.get_flag_client", return_value=fake_client),
        ):
            delay = telemetry_module._get_bsp_schedule_delay_ms("controller-manager-service")
        assert delay == 500
        # The service identity must reach the eval context as the "service" attribute
        # so flagd targeting resolves per service.
        ctx = fake_client.get_integer_value.call_args.args[2]
        assert ctx.attributes.get("service") == "controller-manager-service"

    def test_do_init_passes_schedule_delay_to_bsp(self):
        """_do_init must wire the resolved delay into the BatchSpanProcessor."""
        with (
            patch.object(telemetry_module, "_get_bsp_schedule_delay_ms", return_value=500) as mock_get,
            patch.object(telemetry_module, "BatchSpanProcessor") as mock_bsp,
            patch.object(telemetry_module, "OTLPSpanExporter"),
            patch.object(telemetry_module, "TracerProvider"),
            patch.object(telemetry_module, "get_otel_resource"),
            patch.object(telemetry_module.trace, "set_tracer_provider"),
        ):
            telemetry_module._do_init()
        mock_get.assert_called_once()
        assert mock_bsp.call_args.kwargs["schedule_delay_millis"] == 500


class TestEnsureInitialized:
    """Tests for _ensure_initialized."""

    def test_idempotent(self):
        telemetry_module._initialized = True

        # Should be a no-op when already initialized
        telemetry_module._ensure_initialized()

        assert telemetry_module._initialized is True

    @patch.object(telemetry_module, "_do_init")
    def test_calls_do_init_when_not_initialized(self, mock_do_init):
        telemetry_module._initialized = False
        telemetry_module._test_mode = False

        telemetry_module._ensure_initialized()

        mock_do_init.assert_called_once()
        assert telemetry_module._initialized is True
