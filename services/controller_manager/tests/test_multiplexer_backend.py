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
        a1 = _make_adapter("mock", serials=["AA:AA"])
        a2 = _make_adapter("hidapi", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        result = mux.get_connected_controllers()

        assert result == ["AA:AA"]
        assert mux._serial_to_adapter["AA:AA"] is a1

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_passes_force_to_discover(self, mock_metrics):
        a1 = _make_adapter(serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])

        mux.get_connected_controllers(force_rescan=True)

        a1.discover.assert_called_once_with(force=True)

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
