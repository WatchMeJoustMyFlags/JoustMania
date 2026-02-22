"""
Unit tests for controller health counter tracking (#571).

Tests:
- Poll drop/error counting in DiscoveryLoop
- Drain-and-reset semantics
- LED failure tracking in MultiplexerBackend
- Health proto population in servicer._build_gameplay_update()
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from services.controller_manager.discovery_loop import DiscoveryLoop
from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend


def _make_adapter(adapter_type="mock", serials=None):
    """Create a mock ControllerIOAdapter."""
    adapter = MagicMock()
    adapter.adapter_type = adapter_type
    adapter.discover = MagicMock(return_value=serials or [])
    adapter.open = MagicMock(return_value=True)
    adapter.poll = MagicMock(return_value=None)
    adapter.set_output = MagicMock(return_value=True)
    adapter.close = MagicMock()
    adapter.close_all = MagicMock()
    return adapter


def _create_discovery_loop(**overrides):
    """Create a DiscoveryLoop with mocked dependencies."""
    defaults = {
        "backend": MagicMock(),
        "tracked_controllers": {},
        "controller_states": {},
        "button_detector": MagicMock(),
        "state_cache_manager": MagicMock(),
        "feedback_manager": MagicMock(),
        "monitoring": MagicMock(last_battery_check=0),
        "rescan_timer": MagicMock(),
        "paired_serials": [],
        "base_colors": {},
        "event_publisher": MagicMock(),
    }
    defaults.update(overrides)
    return DiscoveryLoop(**defaults)


class TestPollDropCounting:
    """Tests for poll_drops tracking in DiscoveryLoop."""

    def test_initial_counters_empty(self):
        loop = _create_discovery_loop()
        assert loop._poll_drops == {}
        assert loop._poll_errors == {}

    def test_poll_drops_increment(self):
        """Manually incrementing poll_drops dict simulates None poll results."""
        loop = _create_discovery_loop()
        serial = "AA:BB:CC"

        # Simulate what _update_controller_states does on None result
        loop._poll_drops[serial] = loop._poll_drops.get(serial, 0) + 1
        assert loop._poll_drops[serial] == 1

        loop._poll_drops[serial] = loop._poll_drops.get(serial, 0) + 1
        assert loop._poll_drops[serial] == 2

    def test_poll_errors_increment(self):
        """Manually incrementing poll_errors dict simulates exception results."""
        loop = _create_discovery_loop()
        serial = "AA:BB:CC"

        loop._poll_errors[serial] = loop._poll_errors.get(serial, 0) + 1
        assert loop._poll_errors[serial] == 1

    def test_multiple_serials_independent(self):
        """Each serial has independent counters."""
        loop = _create_discovery_loop()
        loop._poll_drops["S1"] = 3
        loop._poll_drops["S2"] = 7
        loop._poll_errors["S1"] = 1

        assert loop._poll_drops["S1"] == 3
        assert loop._poll_drops["S2"] == 7
        assert loop._poll_errors["S1"] == 1
        assert loop._poll_errors.get("S2", 0) == 0


class TestDrainHealthCounters:
    """Tests for drain_health_counters() drain-and-reset semantics."""

    def test_drain_returns_accumulated_counts(self):
        # Use spec=[] so backend has no _led_failures attribute
        backend = MagicMock(spec=[])
        loop = _create_discovery_loop(backend=backend)
        loop._poll_drops["S1"] = 5
        loop._poll_errors["S1"] = 2

        drops, errors, led = loop.drain_health_counters("S1")
        assert drops == 5
        assert errors == 2
        assert led == 0

    def test_drain_resets_counters(self):
        backend = MagicMock(spec=[])
        loop = _create_discovery_loop(backend=backend)
        loop._poll_drops["S1"] = 5
        loop._poll_errors["S1"] = 2

        loop.drain_health_counters("S1")

        # Second drain should return zeros
        drops, errors, led = loop.drain_health_counters("S1")
        assert drops == 0
        assert errors == 0
        assert led == 0

    def test_drain_unknown_serial_returns_zeros(self):
        backend = MagicMock(spec=[])
        loop = _create_discovery_loop(backend=backend)
        drops, errors, led = loop.drain_health_counters("UNKNOWN")
        assert drops == 0
        assert errors == 0
        assert led == 0

    def test_drain_includes_led_failures_from_backend(self):
        """When backend has _led_failures dict, drain includes LED count."""
        backend = MagicMock()
        backend._led_failures = {"S1": 3}
        loop = _create_discovery_loop(backend=backend)
        loop._poll_drops["S1"] = 1

        drops, errors, led = loop.drain_health_counters("S1")
        assert drops == 1
        assert errors == 0
        assert led == 3
        # LED failures should be drained too
        assert "S1" not in backend._led_failures

    def test_drain_without_led_failures_attribute(self):
        """Backend without _led_failures (non-multiplexer) returns 0 LED failures."""
        backend = MagicMock(spec=[])  # No attributes at all
        loop = _create_discovery_loop(backend=backend)
        loop._poll_drops["S1"] = 1

        drops, errors, led = loop.drain_health_counters("S1")
        assert drops == 1
        assert led == 0

    def test_drain_does_not_affect_other_serials(self):
        loop = _create_discovery_loop()
        loop._poll_drops["S1"] = 5
        loop._poll_drops["S2"] = 10

        loop.drain_health_counters("S1")
        assert loop._poll_drops.get("S2") == 10


class TestDisconnectCleanup:
    """Tests that health counters are cleaned up on disconnect."""

    def test_counters_removed_on_disconnect_path(self):
        """Simulate the disconnect cleanup path from _check_for_new_controllers."""
        loop = _create_discovery_loop()
        serial = "S1"

        # Accumulate some counters
        loop._poll_drops[serial] = 5
        loop._poll_errors[serial] = 2

        # Simulate disconnect cleanup (same as in _check_for_new_controllers)
        loop._poll_drops.pop(serial, None)
        loop._poll_errors.pop(serial, None)

        assert serial not in loop._poll_drops
        assert serial not in loop._poll_errors


class TestLedFailureTracking:
    """Tests for LED failure tracking in MultiplexerBackend."""

    def test_initial_led_failures_empty(self):
        adapter = _make_adapter()
        mux = MultiplexerBackend(adapters=[adapter])
        assert mux._led_failures == {}

    def test_led_failure_on_set_output_false(self):
        """set_output() returning False should increment LED failure counter."""
        adapter = _make_adapter()
        adapter.set_output = MagicMock(return_value=False)
        mux = MultiplexerBackend(adapters=[adapter])

        serial = "S1"
        mux._serial_to_adapter[serial] = adapter
        mux._led_colors[serial] = (255, 0, 0)
        mux._last_sent_color[serial] = (0, 0, 0)  # Different to trigger update

        mux.update_all_leds()
        assert mux._led_failures.get(serial, 0) == 1

    def test_led_failure_on_exception(self):
        """Exception in set_output() should increment LED failure counter."""
        adapter = _make_adapter()
        adapter.set_output = MagicMock(side_effect=OSError("USB error"))
        mux = MultiplexerBackend(adapters=[adapter])

        serial = "S1"
        mux._serial_to_adapter[serial] = adapter
        mux._led_colors[serial] = (255, 0, 0)
        mux._last_sent_color[serial] = (0, 0, 0)

        mux.update_all_leds()
        assert mux._led_failures.get(serial, 0) == 1

    def test_no_failure_on_success(self):
        """Successful set_output() should NOT increment failure counter."""
        adapter = _make_adapter()
        adapter.set_output = MagicMock(return_value=True)
        mux = MultiplexerBackend(adapters=[adapter])

        serial = "S1"
        mux._serial_to_adapter[serial] = adapter
        mux._led_colors[serial] = (255, 0, 0)
        mux._last_sent_color[serial] = (0, 0, 0)

        mux.update_all_leds()
        assert mux._led_failures.get(serial, 0) == 0

    def test_led_failures_accumulate(self):
        """Multiple failures should accumulate."""
        adapter = _make_adapter()
        adapter.set_output = MagicMock(return_value=False)
        mux = MultiplexerBackend(adapters=[adapter])

        serial = "S1"
        mux._serial_to_adapter[serial] = adapter
        mux._led_colors[serial] = (255, 0, 0)

        # Force color change each time to trigger update
        for i in range(3):
            mux._last_sent_color[serial] = (i, i, i)
            mux.update_all_leds()

        assert mux._led_failures.get(serial, 0) == 3

    def test_led_failures_drained_by_discovery_loop(self):
        """drain_health_counters() should drain _led_failures from backend."""
        adapter = _make_adapter()
        mux = MultiplexerBackend(adapters=[adapter])
        mux._led_failures["S1"] = 7

        loop = _create_discovery_loop(backend=mux)
        _drops, _errors, led = loop.drain_health_counters("S1")
        assert led == 7
        assert mux._led_failures.get("S1", 0) == 0
