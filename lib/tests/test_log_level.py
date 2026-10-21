"""log_level flag: flag wins, env fallback, default, runtime change (#1788, FR-150, FR-151)."""

import logging
from unittest.mock import create_autospec, patch

import pytest
from openfeature.client import OpenFeatureClient
from openfeature.flag_evaluation import FlagEvaluationDetails, Reason

from lib import log_level


def _details(value: str, reason: Reason = Reason.STATIC) -> FlagEvaluationDetails:
    return FlagEvaluationDetails(flag_key="log_level", value=value, reason=reason)


@pytest.fixture
def flagd():
    """A fake flagd client; set .return_value of get_string_details per test."""
    client = create_autospec(OpenFeatureClient, instance=True)
    with (
        patch("lib.feature_flags.init_flag_domain"),
        patch("lib.feature_flags.get_flag_client", return_value=client),
    ):
        yield client


@pytest.fixture(autouse=True)
def clean_logger(monkeypatch):
    root = logging.getLogger()
    saved = root.level
    log_level.reset_for_tests()
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    yield
    root.setLevel(saved)
    log_level.reset_for_tests()


def test_flag_wins_over_env(flagd, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    flagd.get_string_details.return_value = _details("debug")
    assert log_level.resolve_log_level() == logging.DEBUG


@pytest.mark.parametrize("reason", [Reason.ERROR])
def test_env_used_when_flag_unresolved(flagd, monkeypatch, reason):
    monkeypatch.setenv("LOG_LEVEL", "warning")
    flagd.get_string_details.return_value = _details("", reason)
    assert log_level.resolve_log_level() == logging.WARNING


def test_env_used_when_flagd_raises(flagd, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    flagd.get_string_details.side_effect = RuntimeError("flagd down")
    assert log_level.resolve_log_level() == logging.DEBUG


def test_default_info_without_flag_and_env(flagd):
    flagd.get_string_details.return_value = _details("", Reason.ERROR)
    assert log_level.resolve_log_level() == logging.INFO


def test_unknown_flag_value_falls_back_to_env(flagd, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "error")
    flagd.get_string_details.return_value = _details("loud")
    assert log_level.resolve_log_level() == logging.ERROR


def test_runtime_change_applies_without_restart(flagd):
    handler = logging.NullHandler()
    log_level.register_handler(handler)
    flagd.get_string_details.return_value = _details("info")
    log_level.refresh_log_level()
    assert logging.getLogger().level == logging.INFO
    assert handler.level == logging.INFO

    flagd.get_string_details.return_value = _details("debug")
    log_level.refresh_log_level()
    assert logging.getLogger().level == logging.DEBUG
    assert handler.level == logging.DEBUG


def test_floor_lowers_level(flagd):
    flagd.get_string_details.return_value = _details("warning")
    log_level.register_floor(lambda: logging.DEBUG)
    assert log_level.refresh_log_level() == logging.DEBUG
