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

import pytest  # noqa: E402 - telemetry must be disabled first
import pytest_asyncio  # noqa: E402

from services.menu.tests.servicer_harness import cancel_background_tasks, make_real_servicer  # noqa: E402


@pytest_asyncio.fixture
async def real_servicer():
    """A real MenuServicer with real StateManager and handlers; only I/O edges autospec'd (#1358)."""
    servicer = make_real_servicer()
    yield servicer
    await cancel_background_tasks(servicer)


@pytest.fixture(autouse=True)
def _no_real_flagd_provider(monkeypatch):
    """Keep a real FlagdProvider out of the unit tests (#2232, after #2226).

    The menu servicer calls ``init_flag_domain`` for four domains; unpatched, each installs
    an in-process FlagdProvider whose gRPC sync thread retries ``flagd:8015`` for the whole
    session. Without a provider, flag reads return their defaults.
    """
    monkeypatch.setattr("lib.feature_flags.init_flag_domain", lambda _domain: None)
    monkeypatch.setattr("services.menu.menu_setup.init_flag_domain", lambda _domain: None)
