"""
Pytest conftest for performance tests.

Handles OTEL disabling and environment setup.
"""

import os
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
from lib.otel_metrics import disable_metrics_for_tests
from lib.telemetry import disable_telemetry_for_tests

disable_telemetry_for_tests()
disable_metrics_for_tests()

# Set environment overrides for fast tests (no countdown delay, fast rainbow)
os.environ.setdefault("COUNTDOWN_DURATION_SECONDS", "0")
os.environ.setdefault("WINNER_RAINBOW_DURATION_MS", "100")

from helpers import TimingResult


@pytest.fixture
def timing_result():
    """Fixture providing a TimingResult instance for collecting frame times."""
    return TimingResult()
