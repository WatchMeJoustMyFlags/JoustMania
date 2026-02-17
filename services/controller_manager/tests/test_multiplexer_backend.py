"""
Unit tests for MultiplexerBackend — composite wrapper that routes
per-controller operations through ControllerIOAdapter instances.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

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


class TestMultiplexerInit:
    def test_requires_at_least_one_adapter(self):
        with pytest.raises(ValueError, match="at least one adapter"):
            MultiplexerBackend(adapters=[])

    def test_accepts_single_adapter(self):
        adapter = _make_adapter()
        mux = MultiplexerBackend(adapters=[adapter])
        assert len(mux.adapters) == 1

    def test_accepts_multiple_adapters(self):
        adapters = [_make_adapter("mock"), _make_adapter("hidapi")]
        mux = MultiplexerBackend(adapters=adapters)
        assert len(mux.adapters) == 2

    def test_exposes_adapters_property(self):
        a1 = _make_adapter("mock")
        a2 = _make_adapter("hidapi")
        mux = MultiplexerBackend(adapters=[a1, a2])
        assert mux.adapters[0] is a1
        assert mux.adapters[1] is a2

    def test_bt_discovery_default_none(self):
        mux = MultiplexerBackend(adapters=[_make_adapter()])
        assert mux.bt_discovery is None

    def test_bt_discovery_stored(self):
        discovery = MagicMock()
        mux = MultiplexerBackend(adapters=[_make_adapter()], bt_discovery=discovery)
        assert mux.bt_discovery is discovery


class TestMultiplexerInitialize:
    @pytest.mark.asyncio
    async def test_discovers_and_opens(self):
        adapter = _make_adapter(serials=["AA:AA", "BB:BB"])
        mux = MultiplexerBackend(adapters=[adapter])

        result = await mux.initialize()

        assert result is True
        adapter.discover.assert_called_once()
        assert adapter.open.call_count == 2

    @pytest.mark.asyncio
    async def test_handles_adapter_exception(self):
        a1 = _make_adapter()
        a1.discover.side_effect = RuntimeError("boom")
        a2 = _make_adapter(serials=["BB:BB"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        result = await mux.initialize()

        assert result is True  # a2 succeeded

    @pytest.mark.asyncio
    async def test_returns_false_if_all_fail(self):
        a1 = _make_adapter()
        a1.discover.side_effect = RuntimeError("boom")
        mux = MultiplexerBackend(adapters=[a1])

        assert await mux.initialize() is False


class TestMultiplexerGetConnectedControllers:
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_merges_serials_from_adapters(self, mock_metrics):
        a1 = _make_adapter("mock", serials=["AA:AA"])
        a2 = _make_adapter("hidapi", serials=["BB:BB"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        result = mux.get_connected_controllers()

        assert sorted(result) == ["AA:AA", "BB:BB"]

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_deduplicates_across_adapters(self, mock_metrics):
        a1 = _make_adapter("psmove", serials=["AA:AA"])
        a2 = _make_adapter("hidapi", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        result = mux.get_connected_controllers()

        assert result == ["AA:AA"]
        # With no targeting, first real adapter wins
        assert mux._serial_to_adapter["AA:AA"] is a1

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_passes_force_to_discover(self, mock_metrics):
        a1 = _make_adapter(serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])

        mux.get_connected_controllers(force_rescan=True)

        a1.discover.assert_called_once_with(force=True, verify_only=False)

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_cleans_stale_state_on_disconnect(self, mock_metrics):
        a1 = _make_adapter(serials=["AA:AA", "BB:BB"])
        mux = MultiplexerBackend(adapters=[a1])

        mux.get_connected_controllers()
        mux._led_colors["BB:BB"] = (255, 0, 0)
        mux._rumble["BB:BB"] = 128

        # BB:BB disconnects
        a1.discover.return_value = ["AA:AA"]
        mux.get_connected_controllers()

        assert "BB:BB" not in mux._led_colors
        assert "BB:BB" not in mux._rumble

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_updates_backend_info_metric(self, mock_metrics):
        a1 = _make_adapter("mock", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])

        mux.get_connected_controllers()

        mock_metrics.controller_backend_info.labels.assert_called_with(serial="AA:AA", backend="mock")


class TestMultiplexerRouting:
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def _setup_two_adapters(self, mock_metrics):
        a1 = _make_adapter("mock", serials=["AA:AA"])
        a2 = _make_adapter("hidapi", serials=["BB:BB"])
        a1.poll.return_value = {"serial": "AA:AA"}
        mux = MultiplexerBackend(adapters=[a1, a2])
        mux.get_connected_controllers()
        return mux, a1, a2

    @pytest.mark.asyncio
    async def test_routes_poll_to_correct_adapter(self):
        mux, a1, a2 = self._setup_two_adapters()

        result = await mux.get_controller_state("AA:AA")

        assert result == {"serial": "AA:AA"}
        a1.poll.assert_called_once_with("AA:AA")
        a2.poll.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_led_stores_color_centrally(self):
        mux, a1, a2 = self._setup_two_adapters()

        await mux.set_led_color("AA:AA", 255, 0, 0)

        assert mux._led_colors["AA:AA"] == (255, 0, 0)
        a1.set_output.assert_called_once_with("AA:AA", 255, 0, 0, 0)

    @pytest.mark.asyncio
    async def test_set_rumble_preserves_led_color(self):
        """Rumble call should include current LED color."""
        mux, a1, a2 = self._setup_two_adapters()

        await mux.set_led_color("AA:AA", 100, 200, 50)
        a1.set_output.reset_mock()

        await mux.set_rumble("AA:AA", 128)

        a1.set_output.assert_called_once_with("AA:AA", 100, 200, 50, 128)
        assert mux._rumble["AA:AA"] == 128

    @pytest.mark.asyncio
    async def test_set_led_preserves_rumble(self):
        """LED color call should include current rumble value."""
        mux, a1, a2 = self._setup_two_adapters()

        await mux.set_rumble("AA:AA", 64)
        a1.set_output.reset_mock()

        await mux.set_led_color("AA:AA", 0, 255, 0)

        a1.set_output.assert_called_once_with("AA:AA", 0, 255, 0, 64)

    def test_effect_active_managed_centrally(self):
        mux, a1, a2 = self._setup_two_adapters()

        mux.set_effect_active("AA:AA", True)
        assert "AA:AA" in mux._effect_active

        mux.set_effect_active("AA:AA", False)
        assert "AA:AA" not in mux._effect_active

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown_serial(self):
        mux, _, _ = self._setup_two_adapters()
        assert await mux.get_controller_state("ZZ:ZZ") is None

    @pytest.mark.asyncio
    async def test_returns_false_for_unknown_serial_set_led(self):
        mux, _, _ = self._setup_two_adapters()
        assert await mux.set_led_color("ZZ:ZZ", 0, 0, 0) is False

    @pytest.mark.asyncio
    async def test_returns_false_for_unknown_serial_set_rumble(self):
        mux, _, _ = self._setup_two_adapters()
        assert await mux.set_rumble("ZZ:ZZ", 0) is False

    def test_set_effect_active_noop_for_unknown(self):
        mux, _, _ = self._setup_two_adapters()
        # Should not raise
        mux.set_effect_active("ZZ:ZZ", True)


class TestMultiplexerUpdateAllLeds:
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_sends_keepalive(self, mock_metrics):
        a1 = _make_adapter("mock", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])
        mux.get_connected_controllers()
        mux._led_colors["AA:AA"] = (255, 0, 0)
        mux._last_led_update["AA:AA"] = 0  # Force keepalive

        count = mux.update_all_leds()

        assert count == 1
        a1.set_output.assert_called_once_with("AA:AA", 255, 0, 0, 0)

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_skips_effect_active(self, mock_metrics):
        a1 = _make_adapter("mock", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])
        mux.get_connected_controllers()
        mux._led_colors["AA:AA"] = (255, 0, 0)
        mux._effect_active.add("AA:AA")
        mux._last_led_update["AA:AA"] = 0

        count = mux.update_all_leds()

        assert count == 0
        a1.set_output.assert_not_called()

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_includes_rumble_in_keepalive(self, mock_metrics):
        a1 = _make_adapter("mock", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])
        mux.get_connected_controllers()
        mux._led_colors["AA:AA"] = (255, 0, 0)
        mux._rumble["AA:AA"] = 128
        mux._last_led_update["AA:AA"] = 0

        mux.update_all_leds()

        a1.set_output.assert_called_once_with("AA:AA", 255, 0, 0, 128)

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_bookkeeping_updated_under_lock(self, mock_metrics):
        """Verify _last_sent_color and _last_led_update are set after set_output."""
        a1 = _make_adapter("mock", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])
        mux.get_connected_controllers()
        mux._led_colors["AA:AA"] = (0, 255, 0)
        mux._last_led_update["AA:AA"] = 0

        mux.update_all_leds()

        assert mux._last_sent_color["AA:AA"] == (0, 255, 0)
        assert mux._last_led_update["AA:AA"] > 0

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_keepalive_interval_is_two_seconds(self, mock_metrics):
        """Keepalive fires at 2s, not 4s (safety margin for hardware LED timeout)."""
        import time

        a1 = _make_adapter("mock", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])
        mux.get_connected_controllers()
        mux._led_colors["AA:AA"] = (255, 0, 0)
        mux._last_sent_color["AA:AA"] = (255, 0, 0)  # No color change
        mux._last_led_update["AA:AA"] = time.time() - 2.5  # 2.5s ago

        count = mux.update_all_leds()

        assert count == 1  # Keepalive should fire at 2s

    @pytest.mark.asyncio
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    async def test_set_led_skips_io_when_lock_held(self, mock_metrics):
        """set_led_color() skips USB write when _led_lock is held by batch."""
        a1 = _make_adapter("mock", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])
        mux.get_connected_controllers()

        # Hold the lock (simulating update_all_leds batch in progress)
        mux._led_lock.acquire()
        try:
            result = await mux.set_led_color("AA:AA", 0, 255, 0)
        finally:
            mux._led_lock.release()

        # Returns True (optimistic) but doesn't call set_output
        assert result is True
        a1.set_output.assert_not_called()
        # Color is stored for next batch cycle
        assert mux._led_colors["AA:AA"] == (0, 255, 0)

    @pytest.mark.asyncio
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    async def test_set_rumble_skips_io_when_lock_held(self, mock_metrics):
        """set_rumble() skips USB write when _led_lock is held by batch."""
        a1 = _make_adapter("mock", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])
        mux.get_connected_controllers()

        mux._led_lock.acquire()
        try:
            result = await mux.set_rumble("AA:AA", 128)
        finally:
            mux._led_lock.release()

        assert result is True
        a1.set_output.assert_not_called()
        assert mux._rumble["AA:AA"] == 128


class TestMultiplexerShutdown:
    @pytest.mark.asyncio
    async def test_calls_close_all_on_adapters(self):
        a1 = _make_adapter()
        a2 = _make_adapter()
        mux = MultiplexerBackend(adapters=[a1, a2])

        await mux.shutdown()

        a1.close_all.assert_called_once()
        a2.close_all.assert_called_once()

    @pytest.mark.asyncio
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    async def test_clears_all_state(self, mock_metrics):
        a1 = _make_adapter(serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])
        mux.get_connected_controllers()
        mux._led_colors["AA:AA"] = (255, 0, 0)
        mux._rumble["AA:AA"] = 64

        await mux.shutdown()

        assert len(mux._serial_to_adapter) == 0
        assert len(mux._led_colors) == 0
        assert len(mux._rumble) == 0

    @pytest.mark.asyncio
    async def test_handles_adapter_exception(self):
        a1 = _make_adapter()
        a2 = _make_adapter()
        a1.close_all.side_effect = RuntimeError("boom")
        mux = MultiplexerBackend(adapters=[a1, a2])

        await mux.shutdown()

        a2.close_all.assert_called_once()


class TestMultiplexerConnectDisconnect:
    @pytest.mark.asyncio
    async def test_connect_tries_adapters(self):
        a1 = _make_adapter()
        a2 = _make_adapter()
        a1.open.return_value = False
        a2.open.return_value = True
        mux = MultiplexerBackend(adapters=[a1, a2])

        result = await mux.connect_controller("SERIAL01")

        assert result is True
        a1.open.assert_called_once_with("SERIAL01")
        a2.open.assert_called_once_with("SERIAL01")

    @pytest.mark.asyncio
    async def test_connect_returns_false_if_none_succeed(self):
        a1 = _make_adapter()
        a1.open.return_value = False
        mux = MultiplexerBackend(adapters=[a1])

        result = await mux.connect_controller("SERIAL01")

        assert result is False

    @pytest.mark.asyncio
    async def test_connect_stops_on_first_success(self):
        a1 = _make_adapter()
        a2 = _make_adapter()
        a1.open.return_value = True
        mux = MultiplexerBackend(adapters=[a1, a2])

        await mux.connect_controller("SERIAL01")

        a2.open.assert_not_called()

    @pytest.mark.asyncio
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    async def test_disconnect_cleans_up_state(self, mock_metrics):
        a1 = _make_adapter(serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])
        mux.get_connected_controllers()
        mux._led_colors["AA:AA"] = (255, 0, 0)
        mux._rumble["AA:AA"] = 64

        result = await mux.disconnect_controller("AA:AA")

        assert result is True
        a1.close.assert_called_once_with("AA:AA")
        assert "AA:AA" not in mux._serial_to_adapter
        assert "AA:AA" not in mux._led_colors
        assert "AA:AA" not in mux._rumble

    @pytest.mark.asyncio
    async def test_disconnect_unknown_returns_false(self):
        a1 = _make_adapter()
        mux = MultiplexerBackend(adapters=[a1])

        result = await mux.disconnect_controller("ZZ:ZZ")

        assert result is False


class TestMultiplexerScanControllers:
    @pytest.mark.asyncio
    async def test_scan_returns_results(self):
        a1 = _make_adapter(serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])

        result = await mux.scan_controllers()

        assert len(result) == 1
        assert result[0]["serial"] == "AA:AA"

    @pytest.mark.asyncio
    async def test_scan_merges_from_multiple_adapters(self):
        a1 = _make_adapter(serials=["AA:AA"])
        a2 = _make_adapter(serials=["BB:BB"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        result = await mux.scan_controllers()

        assert len(result) == 2


class TestRouteControllers:
    """Tests for OpenFeature targeting-based adapter routing."""

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_targeting_routes_to_preferred_adapter(self, mock_metrics):
        """When targeting returns 'hidapi', that adapter should be chosen."""
        a1 = _make_adapter("psmove", serials=["AA:AA"])
        a2 = _make_adapter("hidapi", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        with patch.object(mux, "_resolve_adapter_for_serial", return_value=a2):
            result = mux.get_connected_controllers()

        assert result == ["AA:AA"]
        assert mux._serial_to_adapter["AA:AA"] is a2

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_fallback_when_preferred_not_in_discoverers_and_open_fails(self, mock_metrics):
        """If preferred adapter didn't discover and open() fails, fall back."""
        a1 = _make_adapter("psmove", serials=["AA:AA"])
        a2 = _make_adapter("hidapi", serials=[])  # hidapi didn't find it
        a2.open.return_value = False  # open() also fails
        mux = MultiplexerBackend(adapters=[a1, a2])

        with patch.object(mux, "_resolve_adapter_for_serial", return_value=a2):
            result = mux.get_connected_controllers()

        assert result == ["AA:AA"]
        assert mux._serial_to_adapter["AA:AA"] is a1  # Falls back to psmove

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_open_on_demand_when_preferred_not_in_discoverers(self, mock_metrics):
        """If preferred adapter didn't discover but open() succeeds, use it."""
        a1 = _make_adapter("psmove", serials=["AA:AA"])
        a2 = _make_adapter("hidapi", serials=[])  # hidapi didn't discover
        a2.open.return_value = True  # but open() succeeds (re-enumerates)
        mux = MultiplexerBackend(adapters=[a1, a2])

        with patch.object(mux, "_resolve_adapter_for_serial", return_value=a2):
            result = mux.get_connected_controllers()

        assert result == ["AA:AA"]
        assert mux._serial_to_adapter["AA:AA"] is a2  # Switched via open()
        a2.open.assert_called_with("AA:AA")

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_mock_excluded_from_targeting(self, mock_metrics):
        """Mock-only controllers skip targeting entirely."""
        a1 = _make_adapter("mock", serials=["MOCK01"])
        a2 = _make_adapter("hidapi", serials=[])
        mux = MultiplexerBackend(adapters=[a1, a2])

        resolve_mock = MagicMock()
        with patch.object(mux, "_resolve_adapter_for_serial", resolve_mock):
            result = mux.get_connected_controllers()

        assert result == ["MOCK01"]
        assert mux._serial_to_adapter["MOCK01"] is a1
        resolve_mock.assert_not_called()

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_dynamic_switch_logs(self, mock_metrics, caplog):
        """Switching adapter mid-session should log the change."""
        import logging

        caplog.set_level(logging.INFO)
        a1 = _make_adapter("psmove", serials=["AA:AA"])
        a2 = _make_adapter("hidapi", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        # First discovery: route to psmove (no targeting)
        with patch.object(mux, "_resolve_adapter_for_serial", return_value=None):
            mux.get_connected_controllers()

        assert mux._serial_to_adapter["AA:AA"] is a1

        # Second discovery: targeting switches to hidapi
        with patch.object(mux, "_resolve_adapter_for_serial", return_value=a2):
            mux.get_connected_controllers()

        assert mux._serial_to_adapter["AA:AA"] is a2
        assert "Switched AA:AA: psmove -> hidapi" in caplog.text

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_different_serials_to_different_adapters(self, mock_metrics):
        """Each serial can route to a different adapter independently."""
        a1 = _make_adapter("psmove", serials=["AA:AA", "BB:BB"])
        a2 = _make_adapter("hidapi", serials=["AA:AA", "BB:BB"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        def route(serial):
            return a2 if serial == "AA:AA" else a1

        with patch.object(mux, "_resolve_adapter_for_serial", side_effect=route):
            mux.get_connected_controllers()

        assert mux._serial_to_adapter["AA:AA"] is a2
        assert mux._serial_to_adapter["BB:BB"] is a1

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_single_real_adapter_skips_targeting(self, mock_metrics):
        """When only one real adapter discovers a serial, use it directly."""
        a1 = _make_adapter("psmove", serials=["AA:AA"])
        a2 = _make_adapter("hidapi", serials=[])
        mux = MultiplexerBackend(adapters=[a1, a2])

        # _resolve returns None — but only one real adapter found it
        with patch.object(mux, "_resolve_adapter_for_serial", return_value=None):
            mux.get_connected_controllers()

        assert mux._serial_to_adapter["AA:AA"] is a1

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_opens_handles_for_all_discovering_adapters(self, mock_metrics):
        """Both adapters should get open() called so they have handles ready."""
        a1 = _make_adapter("psmove", serials=["AA:AA"])
        a2 = _make_adapter("hidapi", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        with patch.object(mux, "_resolve_adapter_for_serial", return_value=a1):
            mux.get_connected_controllers()

        a1.open.assert_called_with("AA:AA")
        a2.open.assert_called_with("AA:AA")

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_routing_metric_incremented(self, mock_metrics):
        """Routing decisions should increment the counter metric."""
        a1 = _make_adapter("psmove", serials=["AA:AA"])
        a2 = _make_adapter("hidapi", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        with patch.object(mux, "_resolve_adapter_for_serial", return_value=a2):
            mux.get_connected_controllers()

        mock_metrics.controller_routing_decisions_total.labels.assert_any_call(
            serial="AA:AA",
            adapter="hidapi",
            method="targeted",
        )


class TestResolveAdapterForSerial:
    """Tests for flag evaluation in _resolve_adapter_for_serial."""

    def test_returns_matching_adapter(self):
        a1 = _make_adapter("psmove")
        a2 = _make_adapter("hidapi")
        mux = MultiplexerBackend(adapters=[a1, a2])

        mock_client = MagicMock()
        mock_client.get_string_value.return_value = "hidapi"

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            result = mux._resolve_adapter_for_serial("AA:AA")

        assert result is a2

    def test_returns_none_when_no_match(self):
        a1 = _make_adapter("psmove")
        mux = MultiplexerBackend(adapters=[a1])

        mock_client = MagicMock()
        mock_client.get_string_value.return_value = "nonexistent"

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            result = mux._resolve_adapter_for_serial("AA:AA")

        assert result is None

    def test_returns_none_when_empty_value(self):
        a1 = _make_adapter("psmove")
        mux = MultiplexerBackend(adapters=[a1])

        mock_client = MagicMock()
        mock_client.get_string_value.return_value = ""

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            result = mux._resolve_adapter_for_serial("AA:AA")

        assert result is None

    def test_returns_none_on_exception(self):
        a1 = _make_adapter("psmove")
        mux = MultiplexerBackend(adapters=[a1])

        with patch("lib.feature_flags.get_flag_client", side_effect=RuntimeError("flagd down")):
            result = mux._resolve_adapter_for_serial("AA:AA")

        assert result is None

    def test_passes_serial_as_targeting_key(self):
        a1 = _make_adapter("psmove")
        mux = MultiplexerBackend(adapters=[a1])

        mock_client = MagicMock()
        mock_client.get_string_value.return_value = "psmove"

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            mux._resolve_adapter_for_serial("E0AE5EE111AB")

        call_args = mock_client.get_string_value.call_args
        ctx = call_args[0][2]  # Third positional arg is the EvaluationContext
        assert ctx.targeting_key == "E0AE5EE111AB"


class TestGetAdapterType:
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_returns_adapter_type_for_known_serial(self, mock_metrics):
        a1 = _make_adapter("hidapi", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])
        mux.get_connected_controllers()

        assert mux.get_adapter_type("AA:AA") == "hidapi"

    def test_returns_unknown_for_missing_serial(self):
        mux = MultiplexerBackend(adapters=[_make_adapter()])

        assert mux.get_adapter_type("ZZ:ZZ") == "unknown"


class TestDiscoveryThrottling:
    """Tests for verify_only discovery throttling."""

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_first_call_is_full_discovery(self, mock_metrics):
        """First call should always do full discovery (verify_only=False)."""
        a1 = _make_adapter(serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])

        mux.get_connected_controllers()

        a1.discover.assert_called_once_with(force=False, verify_only=False)

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_rapid_second_call_uses_verify_only(self, mock_metrics):
        """Immediate second call should use verify_only=True."""
        a1 = _make_adapter(serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])

        mux.get_connected_controllers()
        a1.discover.reset_mock()

        mux.get_connected_controllers()

        a1.discover.assert_called_once_with(force=False, verify_only=True)

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_force_always_does_full_discovery(self, mock_metrics):
        """force=True should bypass throttle and use verify_only=False."""
        a1 = _make_adapter(serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])

        # First call to set _last_full_discovery
        mux.get_connected_controllers()
        a1.discover.reset_mock()

        # Immediate force call should still do full discovery
        mux.get_connected_controllers(force_rescan=True)

        a1.discover.assert_called_once_with(force=True, verify_only=False)

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_verify_only_still_returns_controllers(self, mock_metrics):
        """Known controllers should still be reported during verify_only cycles."""
        a1 = _make_adapter(serials=["AA:AA", "BB:BB"])
        mux = MultiplexerBackend(adapters=[a1])

        # First call — full discovery
        result1 = mux.get_connected_controllers()
        assert sorted(result1) == ["AA:AA", "BB:BB"]

        # Second call — verify_only, but adapter still returns known serials
        result2 = mux.get_connected_controllers()
        assert sorted(result2) == ["AA:AA", "BB:BB"]

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    @patch("services.controller_manager.multiplexer.multiplexer_backend.time")
    def test_after_interval_does_full_discovery_again(self, mock_time, mock_metrics):
        """After the throttle interval elapses, full discovery should run again."""
        a1 = _make_adapter(serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])

        # First call at t=0
        mock_time.monotonic.return_value = 0.0
        mock_time.time = MagicMock(return_value=1000.0)
        mux.get_connected_controllers()
        a1.discover.reset_mock()

        # Second call at t=0.1 — within interval, should be verify_only
        mock_time.monotonic.return_value = 0.1
        mux.get_connected_controllers()
        a1.discover.assert_called_once_with(force=False, verify_only=True)
        a1.discover.reset_mock()

        # Third call at t=0.6 — past the 0.5s interval, should be full
        mock_time.monotonic.return_value = 0.6
        mux.get_connected_controllers()
        a1.discover.assert_called_once_with(force=False, verify_only=False)
