"""
Pytest fixtures for menu service tests.
"""

# Disable OpenTelemetry and profiling for tests - must be done before importing service modules
from lib.otel_logging import disable_logging_for_tests
from lib.otel_metrics import disable_metrics_for_tests
from lib.profiling import disable_profiling_for_tests
from lib.telemetry import disable_telemetry_for_tests

disable_telemetry_for_tests()
disable_metrics_for_tests()
disable_logging_for_tests()
disable_profiling_for_tests()
