"""Tests for lib/telemetry.py"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

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
