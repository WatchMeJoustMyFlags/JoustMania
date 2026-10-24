"""Pytest fixtures for the operator pad tests: OTEL off before any service module is imported."""

from lib.otel_logging import disable_logging_for_tests
from lib.otel_metrics import disable_metrics_for_tests
from lib.telemetry import disable_telemetry_for_tests

disable_telemetry_for_tests()
disable_metrics_for_tests()
disable_logging_for_tests()
