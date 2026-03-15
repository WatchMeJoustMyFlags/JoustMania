"""
Pytest conftest for performance tests.

Handles OTEL disabling and environment setup.
"""

import sys
from pathlib import Path

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
tests_dir = test_dir.parent
service_dir = tests_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

# Disable OpenTelemetry for tests (must be before importing service modules)
from lib.otel_logging import disable_logging_for_tests
from lib.otel_metrics import disable_metrics_for_tests
from lib.telemetry import disable_telemetry_for_tests

disable_telemetry_for_tests()
disable_metrics_for_tests()
disable_logging_for_tests()

# Note: Game timing (countdown, rainbow) now controlled via flagd game_settings (Issue #464)
# For performance tests, the RuntimeConfigManager defaults are used unless flagd overrides them.

from helpers import TimingResult


@pytest.fixture
def timing_result():
    """Fixture providing a TimingResult instance for collecting frame times."""
    return TimingResult()
