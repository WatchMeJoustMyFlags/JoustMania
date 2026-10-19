"""
Pytest fixtures for menu service tests.
"""

# Disable OpenTelemetry for tests - must be done before importing service modules
from lib.otel_logging import disable_logging_for_tests
from lib.otel_metrics import disable_metrics_for_tests
from lib.telemetry import disable_telemetry_for_tests

disable_telemetry_for_tests()
disable_metrics_for_tests()
disable_logging_for_tests()

import pytest_asyncio  # noqa: E402 - telemetry must be disabled first

from services.menu.tests.servicer_harness import cancel_background_tasks, make_real_servicer  # noqa: E402


@pytest_asyncio.fixture
async def real_servicer():
    """A real MenuServicer with real StateManager and handlers; only I/O edges autospec'd (#1358)."""
    servicer = make_real_servicer()
    yield servicer
    await cancel_background_tasks(servicer)
