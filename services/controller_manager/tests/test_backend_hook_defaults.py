"""The discovery loop calls the optional backend hooks directly; every backend has inert defaults (#2354)."""

import pytest

from services.controller_manager.mock_hooks import MockFeatureHooks
from services.controller_manager.tests.backend_doubles import InertBackendHooks


@pytest.mark.uc("UC-002", flow="A1")
def test_default_hooks_are_inert():
    hooks = MockFeatureHooks()
    assert hooks.get_adapter_type("S1") == ""
    assert hooks.get_controller_metadata("S1") is None
    assert hooks.rollout_summary() == ("", 0)
    assert hooks.rollout_strategy() == ""
    assert hooks.take_led_failures("S1") == 0


@pytest.mark.uc("UC-002", flow="A1")
def test_multiplexer_take_led_failures_drains_the_counter():
    from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend

    mux = MultiplexerBackend.__new__(MultiplexerBackend)
    mux._led_failures = {"S1": 3}
    assert mux.take_led_failures("S1") == 3
    assert mux.take_led_failures("S1") == 0


def test_inert_backend_double_inherits_the_defaults():
    assert InertBackendHooks().rollout_summary() == ("", 0)
