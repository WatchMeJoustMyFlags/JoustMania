"""
Pytest fixtures for python_hid tests.
"""

import sys
from types import ModuleType

# Disable OpenTelemetry and profiling for tests - must be done before importing service modules
from lib.otel_logging import disable_logging_for_tests
from lib.otel_metrics import disable_metrics_for_tests
from lib.profiling import disable_profiling_for_tests
from lib.telemetry import disable_telemetry_for_tests

disable_telemetry_for_tests()
disable_metrics_for_tests()
disable_logging_for_tests()
disable_profiling_for_tests()

# Install a mock `hidraw` module before importing service code,
# since the real hidapi C extension may not be installed in test environments.
_mock_hid = ModuleType("hidraw")
from unittest.mock import MagicMock

_mock_hid.enumerate = MagicMock(return_value=[])
_mock_hid.device = MagicMock
sys.modules.setdefault("hidraw", _mock_hid)
