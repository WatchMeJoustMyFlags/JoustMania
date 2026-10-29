"""The mock-only hooks are inert defaults on every backend and adapter (#2354)."""

import pytest

from services.controller_manager.multiplexer.adapter import ControllerIOAdapter
from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend


class _PlainAdapter(ControllerIOAdapter):
    """A hardware-like adapter: implements only the required I/O."""

    adapter_type = "plain"

    def discover(self, force=False, exclude_serials=None):
        return []

    def open(self, serial):
        return True

    def poll(self, serial):
        return None

    def set_output(self, serial, r, g, b, rumble):
        return True

    def close(self, serial):
        pass


@pytest.mark.uc("UC-046", flow="A10")
def test_adapter_defaults_are_inert():
    """UC-046: an adapter without the mock features has no pattern, latch or overrun."""
    adapter = _PlainAdapter()
    assert adapter.pattern_frame("S", "game", True) is None
    assert adapter.pattern_stream_rate(["S"]) is None
    assert adapter.record_pattern_overrun("game") == 0
    assert adapter.consume_death_frame("S") is None
    assert adapter.consume_trigger_press("S") is False
    assert adapter.release_pattern_stream("game") is None
    assert adapter.pattern_serials == frozenset()


@pytest.mark.uc("UC-046", flow="A10")
def test_multiplexer_routes_mock_hooks_to_plain_adapters_without_probing():
    """UC-046: the multiplexer calls the hooks on any adapter and gets the inert answer."""
    mux = MultiplexerBackend(adapters=[_PlainAdapter()])
    mux._serial_to_adapter["S"] = mux._adapters[0]
    assert mux.pattern_serials() == set()
    assert mux.pattern_frame("S", "game", True) is None
    assert mux.pattern_stream_rate(["S"]) is None
    assert mux.record_pattern_overrun("game") == 0
    assert mux.consume_trigger_press("S") is False
    mux.consume_death_frame("S")
    mux.release_pattern_stream("game")
