"""
Unit tests for MultiplexerBackend — composite wrapper that routes
per-controller operations to the child backend that owns each serial.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend


def _make_child(name="ChildBackend", serials=None):
    """Create a mock child backend."""
    child = AsyncMock()
    child.__class__ = type(name, (), {})
    child.__class__.__name__ = name
    child.get_connected_controllers = MagicMock(return_value=serials or [])
    child.update_all_leds = MagicMock(return_value=0)
    child.set_effect_active = MagicMock()
    return child


class TestMultiplexerInit:
    def test_requires_at_least_one_child(self):
        with pytest.raises(ValueError, match="at least one child"):
            MultiplexerBackend([])

    def test_accepts_single_child(self):
        child = _make_child()
        mux = MultiplexerBackend([child])
        assert len(mux.children) == 1

    def test_accepts_multiple_children(self):
        children = [_make_child("A"), _make_child("B")]
        mux = MultiplexerBackend(children)
        assert len(mux.children) == 2

    def test_exposes_children_property(self):
        child = _make_child()
        mux = MultiplexerBackend([child])
        assert mux.children[0] is child


class TestMultiplexerInitialize:
    @pytest.mark.asyncio
    async def test_calls_all_children(self):
        c1 = _make_child()
        c2 = _make_child()
        c1.initialize.return_value = True
        c2.initialize.return_value = True
        mux = MultiplexerBackend([c1, c2])

        result = await mux.initialize()

        assert result is True
        c1.initialize.assert_awaited_once()
        c2.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_true_if_at_least_one_succeeds(self):
        c1 = _make_child()
        c2 = _make_child()
        c1.initialize.return_value = False
        c2.initialize.return_value = True
        mux = MultiplexerBackend([c1, c2])

        assert await mux.initialize() is True

    @pytest.mark.asyncio
    async def test_returns_false_if_all_fail(self):
        c1 = _make_child()
        c1.initialize.return_value = False
        mux = MultiplexerBackend([c1])

        assert await mux.initialize() is False

    @pytest.mark.asyncio
    async def test_handles_child_exception(self):
        c1 = _make_child()
        c2 = _make_child()
        c1.initialize.side_effect = RuntimeError("boom")
        c2.initialize.return_value = True
        mux = MultiplexerBackend([c1, c2])

        assert await mux.initialize() is True


class TestMultiplexerGetConnectedControllers:
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_merges_serials_from_children(self, mock_metrics):
        c1 = _make_child(serials=["AA:AA", "BB:BB"])
        c2 = _make_child(serials=["CC:CC"])
        mux = MultiplexerBackend([c1, c2])

        result = mux.get_connected_controllers()

        assert sorted(result) == ["AA:AA", "BB:BB", "CC:CC"]

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_deduplicates_serials(self, mock_metrics):
        """First child wins when same serial appears in multiple children."""
        c1 = _make_child(name="Primary", serials=["AA:AA"])
        c2 = _make_child(name="Secondary", serials=["AA:AA"])
        mux = MultiplexerBackend([c1, c2])

        result = mux.get_connected_controllers()

        assert result == ["AA:AA"]
        # First child should own the serial
        assert mux._serial_to_backend["AA:AA"] is c1

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_passes_force_rescan(self, mock_metrics):
        c1 = _make_child(serials=["AA:AA"])
        mux = MultiplexerBackend([c1])

        mux.get_connected_controllers(force_rescan=True)

        c1.get_connected_controllers.assert_called_once_with(True)

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_cleans_stale_mappings(self, mock_metrics):
        c1 = _make_child(serials=["AA:AA", "BB:BB"])
        mux = MultiplexerBackend([c1])

        mux.get_connected_controllers()
        assert "BB:BB" in mux._serial_to_backend

        # Controller disconnects
        c1.get_connected_controllers.return_value = ["AA:AA"]
        mux.get_connected_controllers()

        assert "BB:BB" not in mux._serial_to_backend

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_updates_backend_info_metric(self, mock_metrics):
        c1 = _make_child(name="MockBackend", serials=["AA:AA"])
        mux = MultiplexerBackend([c1])

        mux.get_connected_controllers()

        mock_metrics.controller_backend_info.labels.assert_called_with(serial="AA:AA", backend="MockBackend")
        mock_metrics.controller_backend_info.labels().set.assert_called_with(1)


class TestMultiplexerRouting:
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def _setup_two_children(self, mock_metrics):
        c1 = _make_child(name="BackendA", serials=["AA:AA"])
        c2 = _make_child(name="BackendB", serials=["BB:BB"])
        mux = MultiplexerBackend([c1, c2])
        mux.get_connected_controllers()
        return mux, c1, c2

    @pytest.mark.asyncio
    async def test_routes_get_state_to_correct_child(self):
        mux, c1, c2 = self._setup_two_children()
        c1.get_controller_state.return_value = {"serial": "AA:AA"}

        result = await mux.get_controller_state("AA:AA")

        assert result == {"serial": "AA:AA"}
        c1.get_controller_state.assert_awaited_once_with("AA:AA")
        c2.get_controller_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_routes_set_led_to_correct_child(self):
        mux, c1, c2 = self._setup_two_children()
        c2.set_led_color.return_value = True

        result = await mux.set_led_color("BB:BB", 255, 0, 0)

        assert result is True
        c2.set_led_color.assert_awaited_once_with("BB:BB", 255, 0, 0)
        c1.set_led_color.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_routes_set_rumble_to_correct_child(self):
        mux, c1, c2 = self._setup_two_children()
        c1.set_rumble.return_value = True

        result = await mux.set_rumble("AA:AA", 128)

        assert result is True
        c1.set_rumble.assert_awaited_once_with("AA:AA", 128)

    def test_routes_set_effect_active_to_correct_child(self):
        mux, c1, c2 = self._setup_two_children()

        mux.set_effect_active("BB:BB", True)

        c2.set_effect_active.assert_called_once_with("BB:BB", True)
        c1.set_effect_active.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown_serial(self):
        mux, _, _ = self._setup_two_children()

        result = await mux.get_controller_state("ZZ:ZZ")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_false_for_unknown_serial_set_led(self):
        mux, _, _ = self._setup_two_children()

        result = await mux.set_led_color("ZZ:ZZ", 0, 0, 0)

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_for_unknown_serial_set_rumble(self):
        mux, _, _ = self._setup_two_children()

        result = await mux.set_rumble("ZZ:ZZ", 0)

        assert result is False

    def test_set_effect_active_noop_for_unknown_serial(self):
        mux, c1, c2 = self._setup_two_children()

        # Should not raise
        mux.set_effect_active("ZZ:ZZ", True)

        c1.set_effect_active.assert_not_called()
        c2.set_effect_active.assert_not_called()

    @pytest.mark.asyncio
    async def test_disconnect_routes_to_owner(self):
        mux, c1, c2 = self._setup_two_children()
        c1.disconnect_controller.return_value = True

        result = await mux.disconnect_controller("AA:AA")

        assert result is True
        c1.disconnect_controller.assert_awaited_once_with("AA:AA")

    @pytest.mark.asyncio
    async def test_disconnect_returns_false_for_unknown(self):
        mux, _, _ = self._setup_two_children()

        result = await mux.disconnect_controller("ZZ:ZZ")

        assert result is False


class TestMultiplexerShutdown:
    @pytest.mark.asyncio
    async def test_calls_all_children(self):
        c1 = _make_child(serials=["AA:AA"])
        c2 = _make_child(serials=["BB:BB"])
        mux = MultiplexerBackend([c1, c2])

        await mux.shutdown()

        c1.shutdown.assert_awaited_once()
        c2.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    async def test_clears_routing_table(self, mock_metrics):
        c1 = _make_child(serials=["AA:AA"])
        mux = MultiplexerBackend([c1])
        mux.get_connected_controllers()
        assert len(mux._serial_to_backend) == 1

        await mux.shutdown()

        assert len(mux._serial_to_backend) == 0

    @pytest.mark.asyncio
    async def test_handles_child_exception(self):
        c1 = _make_child()
        c2 = _make_child()
        c1.shutdown.side_effect = RuntimeError("boom")
        mux = MultiplexerBackend([c1, c2])

        # Should not raise
        await mux.shutdown()

        c2.shutdown.assert_awaited_once()


class TestMultiplexerUpdateAllLeds:
    def test_sums_across_children(self):
        c1 = _make_child()
        c2 = _make_child()
        c1.update_all_leds.return_value = 3
        c2.update_all_leds.return_value = 2
        mux = MultiplexerBackend([c1, c2])

        assert mux.update_all_leds() == 5


class TestMultiplexerConnectController:
    @pytest.mark.asyncio
    async def test_tries_each_child(self):
        c1 = _make_child()
        c2 = _make_child()
        c1.connect_controller.return_value = False
        c2.connect_controller.return_value = True
        mux = MultiplexerBackend([c1, c2])

        result = await mux.connect_controller("00:11:22:33:44:55")

        assert result is True
        c1.connect_controller.assert_awaited_once()
        c2.connect_controller.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_false_if_none_succeed(self):
        c1 = _make_child()
        c1.connect_controller.return_value = False
        mux = MultiplexerBackend([c1])

        result = await mux.connect_controller("00:11:22:33:44:55")

        assert result is False

    @pytest.mark.asyncio
    async def test_stops_on_first_success(self):
        c1 = _make_child()
        c2 = _make_child()
        c1.connect_controller.return_value = True
        mux = MultiplexerBackend([c1, c2])

        await mux.connect_controller("00:11:22:33:44:55")

        c2.connect_controller.assert_not_awaited()


class TestMultiplexerScanControllers:
    @pytest.mark.asyncio
    async def test_merges_scan_results(self):
        c1 = _make_child()
        c2 = _make_child()
        c1.scan_controllers.return_value = [{"serial": "AA:AA"}]
        c2.scan_controllers.return_value = [{"serial": "BB:BB"}]
        mux = MultiplexerBackend([c1, c2])

        result = await mux.scan_controllers()

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_handles_child_scan_exception(self):
        c1 = _make_child()
        c2 = _make_child()
        c1.scan_controllers.side_effect = RuntimeError("boom")
        c2.scan_controllers.return_value = [{"serial": "BB:BB"}]
        mux = MultiplexerBackend([c1, c2])

        result = await mux.scan_controllers()

        assert result == [{"serial": "BB:BB"}]
