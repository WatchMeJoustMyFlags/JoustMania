"""Shared test fixtures for lib unit tests.

Disables OTEL telemetry and metrics so unit tests never attempt OTLP exports
(see .claude/rules/development.md).
"""

from lib.otel_metrics import disable_metrics_for_tests

disable_metrics_for_tests()
