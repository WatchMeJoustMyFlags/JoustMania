"""
Pytest fixtures for mobile_gateway tests.
"""

# Disable OpenTelemetry and profiling for tests
from lib.otel_metrics import disable_metrics_for_tests
from lib.profiling import disable_profiling_for_tests
from lib.telemetry import disable_telemetry_for_tests

disable_telemetry_for_tests()
disable_metrics_for_tests()
disable_profiling_for_tests()
