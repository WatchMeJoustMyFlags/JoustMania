"""Producers export spans and metrics over OTLP to the collector (UC-041 step 1).

Both lib.telemetry (spans) and lib.otel_metrics (metrics) build their OTLP gRPC
exporter from OTEL_EXPORTER_OTLP_ENDPOINT, which docker-compose.yml points at
the collector; the collector then fans out to the agent (step 2).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import lib.otel_metrics as otel_metrics
import lib.telemetry as telemetry

COLLECTOR_ENDPOINT = "http://otel-collector:4317"


@pytest.fixture(autouse=True)
def fresh_metrics_state():
    otel_metrics._meter = None
    otel_metrics._initialized = False
    otel_metrics._pending_metrics.clear()
    yield
    otel_metrics._meter = None
    otel_metrics._initialized = False
    otel_metrics._pending_metrics.clear()


@pytest.mark.uc("UC-041", flow="main", steps=[1])
def test_span_exporter_targets_the_collector_endpoint():
    """UC-041 step 1: the span exporter is an OTLP gRPC exporter on OTEL_EXPORTER_OTLP_ENDPOINT."""
    with (
        patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": COLLECTOR_ENDPOINT}),
        patch.object(telemetry, "_get_bsp_schedule_delay_ms", return_value=500),
        patch.object(telemetry, "OTLPSpanExporter") as exporter,
        patch.object(telemetry, "BatchSpanProcessor"),
        patch.object(telemetry, "TracerProvider"),
        patch.object(telemetry, "get_otel_resource"),
        patch.object(telemetry.trace, "set_tracer_provider"),
    ):
        telemetry._do_init()
    exporter.assert_called_once_with(endpoint=COLLECTOR_ENDPOINT, insecure=True)


@pytest.mark.uc("UC-041", flow="main", steps=[1])
def test_metric_exporter_targets_the_collector_endpoint():
    """UC-041 step 1: the metric exporter is an OTLP gRPC exporter on OTEL_EXPORTER_OTLP_ENDPOINT."""
    with (
        patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": COLLECTOR_ENDPOINT}),
        patch.object(otel_metrics, "OTLPMetricExporter") as exporter,
        patch.object(otel_metrics, "PeriodicExportingMetricReader"),
        patch.object(otel_metrics, "MeterProvider"),
        patch.object(otel_metrics, "metrics"),
    ):
        otel_metrics.init_metrics(export_interval_ms=1000)
    exporter.assert_called_once_with(endpoint=COLLECTOR_ENDPOINT, insecure=True)
