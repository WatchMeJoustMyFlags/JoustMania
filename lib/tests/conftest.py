"""Shared test fixtures for lib unit tests.

Disables OTEL telemetry and metrics so unit tests never attempt OTLP exports
(see .claude/rules/development.md).
"""

import pytest

from lib.otel_metrics import disable_metrics_for_tests

disable_metrics_for_tests()


@pytest.fixture(autouse=True)
def _no_real_flagd_provider(monkeypatch):
    """Keep a real FlagdProvider out of the lib unit tests (#2232, after #2226).

    ``init_metrics`` reads the observability flags through ``init_flag_domain``; unpatched,
    that installs an in-process FlagdProvider whose gRPC sync thread retries ``flagd:8015``
    for the whole session. ``test_feature_flags.py`` imports the real function at module
    level and patches ``FlagdProvider`` itself, so it is unaffected.
    """
    monkeypatch.setattr("lib.feature_flags.init_flag_domain", lambda _domain: None)
