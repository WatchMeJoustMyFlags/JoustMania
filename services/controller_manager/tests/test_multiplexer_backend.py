"""
Unit tests for MultiplexerBackend — composite wrapper that routes
per-controller operations through ControllerIOAdapter instances.
"""

import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import ANY, MagicMock, create_autospec, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from services.controller_manager.multiplexer.adapter import ControllerIOAdapter
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
    adapter.get_adapter_for_serial = MagicMock(return_value=None)
    # Plain adapters are their own device owner — no shared inner.
    adapter.inner = None
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
        adapters = [_make_adapter("mock"), _make_adapter("python")]
        mux = MultiplexerBackend(adapters=adapters)
        assert len(mux.adapters) == 2

    def test_exposes_adapters_property(self):
        a1 = _make_adapter("mock")
        a2 = _make_adapter("python")
        mux = MultiplexerBackend(adapters=[a1, a2])
        assert mux.adapters[0] is a1
        assert mux.adapters[1] is a2


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
        """First adapter fails, second succeeds — both get discover() called."""
        a1 = _make_adapter()
        a1.discover.side_effect = RuntimeError("boom")
        a2 = _make_adapter(serials=["BB:BB"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        result = await mux.initialize()

        assert result is True  # a2 succeeded
        # Both adapters attempted discover
        a1.discover.assert_called_once()
        a2.discover.assert_called_once()

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
        a2 = _make_adapter("python", serials=["BB:BB"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        result = mux.get_connected_controllers()

        assert sorted(result) == ["AA:AA", "BB:BB"]

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_both_adapters_discover_independently(self, mock_metrics):
        """All adapters call discover(); when both find the same serial,
        deduplication picks the first non-mock adapter."""
        a1 = _make_adapter("python", serials=["AA:AA"])
        a2 = _make_adapter("python", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        with patch.object(mux, "_resolve_adapter_for_serial", return_value=(None, "default")):
            result = mux.get_connected_controllers()

        assert result == ["AA:AA"]
        # Both adapters called discover() independently
        a1.discover.assert_called_once()
        a2.discover.assert_called_once()
        # First non-mock wins by default
        assert mux._serial_to_adapter["AA:AA"] is a1

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_passes_force_to_discover(self, mock_metrics):
        a1 = _make_adapter(serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])

        mux.get_connected_controllers(force_rescan=True)

        a1.discover.assert_called_once_with(force=True, exclude_serials=ANY)

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


class TestControllerInfoSeriesRemoval:
    """controller_adapter_info / controller_backend_info series go away with the link (#1468)."""

    @pytest.mark.uc("UC-035", flow="main", steps=[6])
    @pytest.mark.uc("UC-002", flow="A4")
    @pytest.mark.uc("UC-035", br=["BR-009"])
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_adapter_change_removes_previous_adapter_series(self, mock_metrics):
        a1 = _make_adapter("mock", serials=["AA:AA"])
        a1.get_adapter_for_serial.return_value = "hci1"
        mux = MultiplexerBackend(adapters=[a1])
        mux.get_connected_controllers()
        mock_metrics.controller_adapter_info.remove.assert_not_called()

        a1.get_adapter_for_serial.return_value = "hci0"
        mux.get_connected_controllers()

        mock_metrics.controller_adapter_info.remove.assert_called_once_with("AA:AA", "hci1")
        mock_metrics.controller_adapter_info.labels.assert_called_with(serial="AA:AA", adapter="hci0")

    @pytest.mark.uc("UC-035", flow="main", steps=[6])
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_backend_change_removes_previous_backend_series(self, mock_metrics):
        a1 = _make_adapter("rust", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])
        mux.get_connected_controllers()

        a1.adapter_type = "python"
        mux.get_connected_controllers()

        mock_metrics.controller_backend_info.remove.assert_called_once_with("AA:AA", "rust")

    @pytest.mark.uc("UC-035", flow="main", steps=[6])
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_vanished_controller_removes_adapter_and_backend_series(self, mock_metrics):
        a1 = _make_adapter("mock", serials=["AA:AA", "BB:BB"])
        a1.get_adapter_for_serial.return_value = "hci1"
        mux = MultiplexerBackend(adapters=[a1])
        mux.get_connected_controllers()

        a1.discover.return_value = ["AA:AA"]
        mux.get_connected_controllers()

        mock_metrics.controller_adapter_info.remove.assert_called_once_with("BB:BB", "hci1")
        mock_metrics.controller_backend_info.remove.assert_called_once_with("BB:BB", "mock")

    @pytest.mark.uc("UC-035", flow="main", steps=[6])
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_vanished_controller_without_led_or_adapter_removes_backend_series(self, mock_metrics):
        """A controller that never got an LED colour nor an HCI adapter still loses its series."""
        a1 = _make_adapter("mock", serials=["AA:AA", "BB:BB"])
        a1.get_adapter_for_serial.return_value = None
        mux = MultiplexerBackend(adapters=[a1])
        mux.get_connected_controllers()
        assert "BB:BB" not in mux._led_colors

        a1.discover.return_value = ["AA:AA"]
        mux.get_connected_controllers()

        mock_metrics.controller_backend_info.remove.assert_called_once_with("BB:BB", "mock")
        mock_metrics.controller_adapter_info.remove.assert_not_called()
        assert "BB:BB" not in mux._last_backend

    @pytest.mark.uc("UC-035", flow="main", steps=[6])
    @pytest.mark.asyncio
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    async def test_metrics_update_after_shutdown_does_not_resurrect_series(self, mock_metrics):
        """A discovery pass holding a pre-shutdown snapshot must not re-export the gone serial."""
        a1 = _make_adapter("mock", serials=["AA:AA"])
        a1.get_adapter_for_serial.return_value = "hci0"
        mux = MultiplexerBackend(adapters=[a1])
        mux.get_connected_controllers()
        stale_seen = dict(mux._serial_to_adapter)

        await mux.shutdown()
        mock_metrics.controller_backend_info.labels.reset_mock()
        mock_metrics.controller_adapter_info.labels.reset_mock()
        mux._update_controller_metrics(stale_seen)

        mock_metrics.controller_backend_info.labels.assert_not_called()
        mock_metrics.controller_adapter_info.labels.assert_not_called()
        assert "AA:AA" not in mux._last_backend
        assert "AA:AA" not in mux._last_adapter

    @pytest.mark.uc("UC-035", flow="main", steps=[6])
    @pytest.mark.asyncio
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    async def test_shutdown_removes_info_series(self, mock_metrics):
        """shutdown() removes the backend / adapter info series of every tracked serial (#1726)."""
        a1 = _make_adapter("mock", serials=["AA:AA"])
        a1.get_adapter_for_serial.return_value = "hci0"
        mux = MultiplexerBackend(adapters=[a1])
        mux.get_connected_controllers()
        mux._update_controller_metrics(dict(mux._serial_to_adapter))

        await mux.shutdown()

        mock_metrics.controller_backend_info.remove.assert_called_once_with("AA:AA", "mock")
        mock_metrics.controller_adapter_info.remove.assert_called_once_with("AA:AA", "hci0")


class TestMultiplexerRouting:
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def _setup_two_adapters(self, mock_metrics):
        a1 = _make_adapter("mock", serials=["AA:AA"])
        a2 = _make_adapter("python", serials=["BB:BB"])
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

    def test_routes_consume_death_frame_to_owning_adapter(self):
        """consume_death_frame is routed to the serial's adapter (#926)."""
        mux, a1, a2 = self._setup_two_adapters()

        mux.consume_death_frame("AA:AA")

        a1.consume_death_frame.assert_called_once_with("AA:AA")
        a2.consume_death_frame.assert_not_called()

    def test_consume_death_frame_unknown_serial_is_noop(self):
        """Unknown serial -> no adapter -> safe no-op (no exception) (#926)."""
        mux, a1, a2 = self._setup_two_adapters()

        mux.consume_death_frame("ZZ:ZZ")  # must not raise

        a1.consume_death_frame.assert_not_called()
        a2.consume_death_frame.assert_not_called()

    def test_consume_death_frame_skips_adapter_without_hook(self):
        """Hardware adapters lacking the hook are skipped, not errored (#926)."""
        mux, a1, a2 = self._setup_two_adapters()
        # Real hardware adapters don't implement consume_death_frame.
        del a1.consume_death_frame

        mux.consume_death_frame("AA:AA")  # must not raise

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


class TestShutdownDuringDiscovery:
    """#1630: discovery builds the routing table in a worker thread. A shutdown()
    that lands while a pass is running must not have that pass reinstall routes
    to the adapters it just closed. The interleaving is forced with events."""

    _METRICS = "services.controller_manager.multiplexer.multiplexer_backend.metrics"

    @staticmethod
    def _gated_adapter(discovered: list[str]):
        """An autospec adapter whose discover() parks until the test releases it."""
        adapter = create_autospec(ControllerIOAdapter, instance=True)
        adapter.adapter_type = "mock"
        adapter.open.return_value = True
        in_discover = threading.Event()
        release = threading.Event()
        gate = {"armed": False}

        def discover(*_args, **_kwargs):
            if gate["armed"]:
                in_discover.set()
                assert release.wait(timeout=5), "test never released discovery"
            return list(discovered)

        adapter.discover.side_effect = discover
        return adapter, gate, in_discover, release

    @pytest.mark.uc("UC-002", flow="main", steps=[3])
    @pytest.mark.asyncio
    async def test_pass_finishing_after_shutdown_installs_no_routes(self):
        adapter, gate, in_discover, release = self._gated_adapter(["AA:AA"])
        mux = MultiplexerBackend(adapters=[adapter])
        result: dict = {}
        with patch(self._METRICS, autospec=True):
            gate["armed"] = True
            worker = threading.Thread(target=lambda: result.update(serials=mux.get_connected_controllers()))
            worker.start()
            assert in_discover.wait(timeout=5)

            # The pass has enumerated (and opened) AA:AA; shut down now.
            await mux.shutdown()
            adapter.close.reset_mock()

            release.set()
            worker.join(timeout=5)

        assert not worker.is_alive()
        assert result["serials"] == []
        assert mux._serial_to_adapter == {}
        assert await mux.get_controller_state("AA:AA") is None
        # The handle the pass held after close_all() is closed again.
        adapter.close.assert_called_once_with("AA:AA")

    @pytest.mark.uc("UC-002", flow="main", steps=[3])
    def test_no_discovery_after_shutdown(self):
        adapter, _gate, _in_discover, _release = self._gated_adapter(["AA:AA"])
        mux = MultiplexerBackend(adapters=[adapter])
        asyncio.run(mux.shutdown())

        assert mux.get_connected_controllers() == []
        adapter.discover.assert_not_called()

    @pytest.mark.asyncio
    async def test_initialize_after_shutdown_reopens(self):
        adapter, _gate, _in_discover, _release = self._gated_adapter(["AA:AA"])
        mux = MultiplexerBackend(adapters=[adapter])
        await mux.shutdown()
        await mux.initialize()
        with patch(self._METRICS, autospec=True):
            assert mux.get_connected_controllers() == ["AA:AA"]


class TestRouteControllers:
    """Tests for OpenFeature targeting-based adapter routing."""

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    @pytest.mark.uc("UC-002", flow="main", steps=[3], br=["BR-001"])
    def test_targeting_routes_to_preferred_adapter(self, mock_metrics):
        """Both adapters discover AA:AA; targeting picks a2, a1's handle is closed."""
        a1 = _make_adapter("python", serials=["AA:AA"])
        a2 = _make_adapter("python", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        with patch.object(mux, "_resolve_adapter_for_serial", return_value=(a2, "targeted")):
            result = mux.get_connected_controllers()

        assert result == ["AA:AA"]
        assert mux._serial_to_adapter["AA:AA"] is a2
        # Losing adapter's handle is closed
        a1.close.assert_called_with("AA:AA")

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_fallback_when_preferred_not_in_discoverers(self, mock_metrics):
        """Preferred adapter didn't discover the serial; tries open() on demand.
        If open() fails, falls back to first non-mock discoverer."""
        a1 = _make_adapter("python", serials=["AA:AA"])
        a2 = _make_adapter("python", serials=[])
        a2.open.return_value = False
        mux = MultiplexerBackend(adapters=[a1, a2])

        with patch.object(mux, "_resolve_adapter_for_serial", return_value=(a2, "targeted")):
            result = mux.get_connected_controllers()

        assert result == ["AA:AA"]
        # Falls back to first non-mock discoverer
        assert mux._serial_to_adapter["AA:AA"] is a1

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_preferred_adapter_open_on_demand(self, mock_metrics):
        """Preferred adapter didn't discover it but open() succeeds."""
        a1 = _make_adapter("python", serials=["AA:AA"])
        a2 = _make_adapter("python", serials=[])
        a2.open.return_value = True
        mux = MultiplexerBackend(adapters=[a1, a2])

        with patch.object(mux, "_resolve_adapter_for_serial", return_value=(a2, "targeted")):
            result = mux.get_connected_controllers()

        assert result == ["AA:AA"]
        assert mux._serial_to_adapter["AA:AA"] is a2
        a2.open.assert_called_with("AA:AA")

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_mock_excluded_from_targeting(self, mock_metrics):
        """Mock-only controllers (len==1, type==mock) skip targeting entirely."""
        a1 = _make_adapter("mock", serials=["MOCK01"])
        a2 = _make_adapter("python", serials=[])
        mux = MultiplexerBackend(adapters=[a1, a2])

        resolve_mock = MagicMock()
        with patch.object(mux, "_resolve_adapter_for_serial", resolve_mock):
            result = mux.get_connected_controllers()

        assert result == ["MOCK01"]
        assert mux._serial_to_adapter["MOCK01"] is a1
        resolve_mock.assert_not_called()

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_dynamic_switch_logs(self, mock_metrics, caplog):
        """Switching adapter mid-session should log the change.

        The dynamic-switch path fires when the old adapter is NOT among
        the current discoverers (it held the serial from a prior cycle).
        """
        import logging

        caplog.set_level(logging.INFO)
        a1 = _make_adapter("python", serials=["AA:AA"])
        a2 = _make_adapter("rust", serials=[])
        mux = MultiplexerBackend(adapters=[a1, a2])

        # First discovery: a1 discovers, gets routed
        with patch.object(mux, "_resolve_adapter_for_serial", return_value=(a1, "targeted")):
            mux.get_connected_controllers()

        assert mux._serial_to_adapter["AA:AA"] is a1

        # Second discovery: only a2 discovers the serial now
        a1.discover.return_value = []
        a2.discover.return_value = ["AA:AA"]
        with patch.object(mux, "_resolve_adapter_for_serial", return_value=(a2, "targeted")):
            mux.get_connected_controllers()

        assert mux._serial_to_adapter["AA:AA"] is a2
        assert "Switched AA:AA: python -> rust" in caplog.text

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    @pytest.mark.uc("UC-002", flow="main", steps=[3], br=["BR-001"])
    def test_dynamic_switch_closes_old_adapter(self, mock_metrics):
        """Switching adapter should close the handle on the old adapter."""
        a1 = _make_adapter("python", serials=["AA:AA"])
        a2 = _make_adapter("python", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        # First discovery: targeting picks a1
        with patch.object(mux, "_resolve_adapter_for_serial", return_value=(a1, "targeted")):
            mux.get_connected_controllers()

        assert mux._serial_to_adapter["AA:AA"] is a1
        # Reset close counts from dedup (a2's handle closed as non-winner)
        a1.close.reset_mock()
        a2.close.reset_mock()

        # Second discovery: targeting switches to a2
        with patch.object(mux, "_resolve_adapter_for_serial", return_value=(a2, "targeted")):
            mux.get_connected_controllers()

        # Old adapter (a1) should have been closed via dynamic switch
        a1.close.assert_called_with("AA:AA")

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    @pytest.mark.uc("UC-002", flow="main", steps=[3], br=["BR-001"])
    def test_different_serials_to_different_adapters(self, mock_metrics):
        """Each serial can route to a different adapter independently."""
        a1 = _make_adapter("python", serials=["AA:AA", "BB:BB"])
        a2 = _make_adapter("python", serials=["AA:AA", "BB:BB"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        def route(serial, _candidate_serials=None):
            return (a2, "targeted") if serial == "AA:AA" else (a1, "targeted")

        with patch.object(mux, "_resolve_adapter_for_serial", side_effect=route):
            mux.get_connected_controllers()

        # AA:AA routed to a2, BB:BB routed to a1
        assert mux._serial_to_adapter["AA:AA"] is a2
        assert mux._serial_to_adapter["BB:BB"] is a1

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_single_real_adapter_skips_targeting(self, mock_metrics):
        """When only one adapter discovers a serial, use it directly."""
        a1 = _make_adapter("python", serials=["AA:AA"])
        a2 = _make_adapter("python", serials=[])
        mux = MultiplexerBackend(adapters=[a1, a2])

        # _resolve returns None — but only one real adapter found it
        with patch.object(mux, "_resolve_adapter_for_serial", return_value=(None, "default")):
            mux.get_connected_controllers()

        assert mux._serial_to_adapter["AA:AA"] is a1

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_routing_metric_incremented(self, mock_metrics):
        """Routing decisions should increment the counter metric."""
        a1 = _make_adapter("python", serials=["AA:AA"])
        a2 = _make_adapter("python", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        with patch.object(mux, "_resolve_adapter_for_serial", return_value=(a2, "targeted")):
            mux.get_connected_controllers()

        mock_metrics.controller_routing_decisions_total.labels.assert_any_call(
            serial="AA:AA",
            adapter="python",
            method="targeted",
        )

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_rollout_method_label_recorded(self, mock_metrics):
        """A rollout-driven winner records the 'rollout' method label."""
        a1 = _make_adapter("python", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])

        with patch.object(mux, "_resolve_adapter_for_serial", return_value=(a1, "rollout")):
            mux.get_connected_controllers()

        mock_metrics.controller_routing_decisions_total.labels.assert_any_call(
            serial="AA:AA",
            adapter="python",
            method="rollout",
        )


class TestUnstableRoutingOnly:
    """Routing-only adapters (unstable) must never win by default/fallback."""

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_unstable_registered_for_targeting_not_discovery(self, mock_metrics):
        a_py = _make_adapter("python")
        a_un = _make_adapter("unstable")
        mux = MultiplexerBackend(adapters=[a_py], routing_only_adapters=[a_un])

        # Reachable as a targeting/rollout destination
        assert mux._adapter_by_type["unstable"] is a_un
        # But not an independent discoverer
        assert a_un not in mux.adapters

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_unstable_excluded_from_fallback_selection(self, mock_metrics):
        """When two non-mock adapters discover a serial and one is unstable,
        the fallback path must pick the non-unstable adapter."""
        a_py = _make_adapter("python", serials=["AA:AA"])
        a_un = _make_adapter("unstable", serials=["AA:AA"])
        # Put unstable FIRST so a naive 'first non-mock' would wrongly pick it.
        mux = MultiplexerBackend(adapters=[a_un, a_py])

        # No targeting/rollout resolves -> fallback selection
        with patch.object(mux, "_resolve_adapter_for_serial", return_value=(None, "default")):
            mux.get_connected_controllers()

        assert mux._serial_to_adapter["AA:AA"] is a_py

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_unstable_only_falls_back_to_itself(self, mock_metrics):
        """If unstable is the ONLY discoverer, adapters[0] fallback still uses it."""
        a_un = _make_adapter("unstable", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a_un])

        with patch.object(mux, "_resolve_adapter_for_serial", return_value=(None, "default")):
            mux.get_connected_controllers()

        assert mux._serial_to_adapter["AA:AA"] is a_un


class TestSharedInnerNotClosed:
    """Switching between a plain adapter and a decorator sharing its inner
    must not close the shared device."""

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_loser_sharing_inner_not_closed(self, mock_metrics):
        from services.controller_manager.multiplexer.unstable_adapter import UnstableAdapter

        a_py = _make_adapter("python", serials=["AA:AA"])
        a_un = UnstableAdapter(a_py)  # shares a_py as inner
        mux = MultiplexerBackend(adapters=[a_py], routing_only_adapters=[a_un])

        # Route the serial to the unstable wrapper; python is the discoverer/loser.
        with patch.object(mux, "_resolve_adapter_for_serial", return_value=(a_un, "targeted")):
            mux.get_connected_controllers()

        assert mux._serial_to_adapter["AA:AA"] is a_un
        # Shared inner must NOT have been closed by the losing python discoverer
        a_py.close.assert_not_called()


class TestIndependentDiscovery:
    """Tests for the independent discovery model where all adapters discover."""

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_all_adapters_discover_independently(self, mock_metrics):
        """Two non-mock adapters both get discover() called."""
        a1 = _make_adapter("python", serials=["AA:AA"])
        a2 = _make_adapter("python", serials=["BB:BB"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        mux.get_connected_controllers()

        a1.discover.assert_called_once()
        a2.discover.assert_called_once()

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_deduplication_non_mock_wins_over_mock(self, mock_metrics):
        """Serial found by both mock and python — non-mock wins."""
        a1 = _make_adapter("mock", serials=["AA:AA"])
        a2 = _make_adapter("python", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        with patch.object(mux, "_resolve_adapter_for_serial", return_value=(None, "default")):
            mux.get_connected_controllers()

        assert mux._serial_to_adapter["AA:AA"] is a2

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_losing_adapter_handle_closed(self, mock_metrics):
        """When two adapters find the same serial, non-winner's handle is closed."""
        a1 = _make_adapter("python", serials=["AA:AA"])
        a2 = _make_adapter("python", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1, a2])

        # Targeting picks a1 as winner
        with patch.object(mux, "_resolve_adapter_for_serial", return_value=(a1, "targeted")):
            mux.get_connected_controllers()

        assert mux._serial_to_adapter["AA:AA"] is a1
        # Loser (a2) has its handle closed
        a2.close.assert_called_with("AA:AA")


def _details(value, reason):
    """Build a fake FlagEvaluationDetails-like object for get_string_details."""
    d = MagicMock()
    d.value = value
    d.reason = reason
    return d


class TestResolveAdapterForSerial:
    """Tests for three-way precedence in _resolve_adapter_for_serial."""

    @pytest.mark.uc("UC-002", flow="main", steps=[3])
    @pytest.mark.uc("UC-052", flow="main", steps=[3])
    def test_explicit_targeting_match_wins(self):
        """A TARGETING_MATCH on bluetooth_backend routes to that adapter."""
        a1 = _make_adapter("python")
        a2 = _make_adapter("rust")
        mux = MultiplexerBackend(adapters=[a1, a2])

        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _details("rust", "TARGETING_MATCH")

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            adapter, method = mux._resolve_adapter_for_serial("AA:AA", ["AA:AA"])

        assert adapter is a2
        assert method == "targeted"

    @pytest.mark.uc("UC-002", flow="main", steps=[3])
    def test_default_variant_is_method_default_not_targeted(self):
        """A non-TARGETING_MATCH reason (default variant) routes as 'default'."""
        a1 = _make_adapter("python")
        mux = MultiplexerBackend(adapters=[a1])

        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _details("python", "DEFAULT")
        # No rollout in play
        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch.object(mux._rollout_router, "target_for", return_value=None),
        ):
            adapter, method = mux._resolve_adapter_for_serial("AA:AA", ["AA:AA"])

        assert adapter is a1
        assert method == "default"

    @pytest.mark.uc("UC-002", flow="main", steps=[3])
    @pytest.mark.uc("UC-050", flow="main", steps=[6])
    def test_rollout_beats_default_when_no_explicit_targeting(self):
        """Rollout target wins over the flag default variant."""
        a_py = _make_adapter("python")
        a_un = _make_adapter("unstable")
        mux = MultiplexerBackend(adapters=[a_py], routing_only_adapters=[a_un])

        mock_client = MagicMock()
        # bluetooth_backend resolves to default variant (no TARGETING_MATCH)
        mock_client.get_string_details.return_value = _details("python", "DEFAULT")

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch.object(mux._rollout_router, "target_for", return_value="unstable"),
        ):
            adapter, method = mux._resolve_adapter_for_serial("AA:AA", ["AA:AA"])

        assert adapter is a_un
        assert method == "rollout"

    @pytest.mark.uc("UC-002", flow="main", steps=[3])
    @pytest.mark.uc("UC-050", flow="A4")
    @pytest.mark.uc("UC-052", flow="main", br=["BR-001"])
    def test_explicit_targeting_beats_rollout(self):
        """Explicit TARGETING_MATCH takes precedence over an active rollout."""
        a_py = _make_adapter("python")
        a_un = _make_adapter("unstable")
        mux = MultiplexerBackend(adapters=[a_py], routing_only_adapters=[a_un])

        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _details("python", "TARGETING_MATCH")

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch.object(mux._rollout_router, "target_for", return_value="unstable") as rollout,
        ):
            adapter, method = mux._resolve_adapter_for_serial("AA:AA", ["AA:AA"])

        assert adapter is a_py
        assert method == "targeted"
        # Explicit match short-circuits before rollout is consulted
        rollout.assert_not_called()

    def test_returns_none_when_no_match(self):
        a1 = _make_adapter("python")
        mux = MultiplexerBackend(adapters=[a1])

        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _details("nonexistent", "DEFAULT")

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch.object(mux._rollout_router, "target_for", return_value=None),
        ):
            adapter, method = mux._resolve_adapter_for_serial("AA:AA", ["AA:AA"])

        assert adapter is None
        assert method == "default"

    def test_returns_none_when_empty_value(self):
        a1 = _make_adapter("python")
        mux = MultiplexerBackend(adapters=[a1])

        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _details("", "DEFAULT")

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch.object(mux._rollout_router, "target_for", return_value=None),
        ):
            adapter, method = mux._resolve_adapter_for_serial("AA:AA", ["AA:AA"])

        assert adapter is None
        assert method == "default"

    def test_returns_none_on_exception(self):
        a1 = _make_adapter("python")
        mux = MultiplexerBackend(adapters=[a1])

        with patch("lib.feature_flags.get_flag_client", side_effect=RuntimeError("flagd down")):
            adapter, method = mux._resolve_adapter_for_serial("AA:AA", ["AA:AA"])

        assert adapter is None
        assert method == "default"

    def test_passes_serial_as_targeting_key(self):
        a1 = _make_adapter("python")
        mux = MultiplexerBackend(adapters=[a1])

        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _details("python", "TARGETING_MATCH")

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            mux._resolve_adapter_for_serial("E0AE5EE111AB", ["E0AE5EE111AB"])

        call_args = mock_client.get_string_details.call_args
        ctx = call_args[0][2]  # Third positional arg is the EvaluationContext
        assert ctx.targeting_key == "E0AE5EE111AB"


class TestGetAdapterType:
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_returns_adapter_type_for_known_serial(self, mock_metrics):
        a1 = _make_adapter("python", serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])
        mux.get_connected_controllers()

        assert mux.get_adapter_type("AA:AA") == "python"

    def test_returns_unknown_for_missing_serial(self):
        mux = MultiplexerBackend(adapters=[_make_adapter()])

        assert mux.get_adapter_type("ZZ:ZZ") == "unknown"


class TestEveryPassIsAFullDiscovery:
    """Every discovery pass enumerates in full (no verify-only pass, #2352)."""

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_back_to_back_passes_both_enumerate(self, mock_metrics):
        a1 = _make_adapter(serials=["AA:AA", "BB:BB"])
        mux = MultiplexerBackend(adapters=[a1])

        assert sorted(mux.get_connected_controllers()) == ["AA:AA", "BB:BB"]
        assert sorted(mux.get_connected_controllers()) == ["AA:AA", "BB:BB"]

        assert a1.discover.call_count == 2
        a1.discover.assert_called_with(force=False, exclude_serials=ANY)
        assert mock_metrics.discovery_full_enumerate_total.inc.call_count == 2

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_force_is_passed_through(self, mock_metrics):
        a1 = _make_adapter(serials=["AA:AA"])
        mux = MultiplexerBackend(adapters=[a1])

        mux.get_connected_controllers(force_rescan=True)

        a1.discover.assert_called_once_with(force=True, exclude_serials=ANY)


class TestLedBatchVersusDiscoveryCleanup:
    """The LED batch and discovery run in separate worker threads (#1447)."""

    @pytest.mark.nfr("NFR-002")
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_batch_skips_a_serial_removed_while_it_waited_for_the_lock(self, _metrics):
        import threading
        import time

        adapter = _make_adapter("mock", serials=["BB:BB"])
        mux = MultiplexerBackend(adapters=[adapter])
        mux.get_connected_controllers()
        mux._led_colors["BB:BB"] = (255, 0, 0)  # colour change pending

        # Discovery holds the lock while it swaps routing and drops BB:BB.
        mux._led_lock.acquire()
        try:
            batch = threading.Thread(target=mux.update_all_leds)
            batch.start()
            time.sleep(0.2)  # the batch is now waiting for the lock
            mux._serial_to_adapter = {}
            mux._cleanup_stale_state({})
        finally:
            mux._led_lock.release()
        batch.join(timeout=2.0)
        assert not batch.is_alive()

        adapter.set_output.assert_not_called()
        assert "BB:BB" not in mux._last_sent_color
        assert "BB:BB" not in mux._last_led_update
        assert "BB:BB" not in mux._led_failures

    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_cleanup_drops_led_failures_of_a_lost_serial(self, _metrics):
        adapter = _make_adapter("mock", serials=["BB:BB"])
        mux = MultiplexerBackend(adapters=[adapter])
        mux.get_connected_controllers()
        mux._led_colors["BB:BB"] = (1, 2, 3)
        mux._led_failures["BB:BB"] = 4
        adapter.discover.return_value = []

        mux.get_connected_controllers()

        assert "BB:BB" not in mux._led_failures


class TestRoutingSwitchKeepsTheController:
    """UC-002 BR-001: a routing change applies on the next cycle without a reconnect."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="main", steps=[3], br=["BR-001"])
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    async def test_adapter_switch_publishes_no_disconnect_or_connect(self, _mock_metrics):
        """Two discovery cycles over a real multiplexer: the serial moves from a1 to a2 and the
        discovery loop publishes no disconnect/connect event and keeps tracking it."""
        from unittest.mock import create_autospec

        from lib.controller_constants import ControllerInfoKey
        from services.controller_manager.button_detector import ButtonDetector
        from services.controller_manager.discovery import PeriodicRescanTimer
        from services.controller_manager.discovery_loop import DiscoveryLoop
        from services.controller_manager.event_publisher import EventPublisher
        from services.controller_manager.feedback_manager import FeedbackManager
        from services.controller_manager.monitoring import ControllerMonitoring
        from services.controller_manager.multiplexer.adapter import ControllerIOAdapter

        def adapter(adapter_type):
            spec = create_autospec(ControllerIOAdapter, instance=True)
            spec.adapter_type = adapter_type
            # Like rust-hid: a serial another adapter holds is not opened (exclude_serials).
            spec.discover.side_effect = lambda force=False, exclude_serials=None: [  # noqa: ARG005
                serial for serial in ["AA:AA"] if serial not in (exclude_serials or [])
            ]
            spec.open.return_value = True
            return spec

        a1, a2 = adapter("python"), adapter("rust")
        mux = MultiplexerBackend(adapters=[a1, a2])
        button_detector = create_autospec(ButtonDetector, instance=True)
        rescan_timer = create_autospec(PeriodicRescanTimer, instance=True)
        rescan_timer.should_force_rescan.return_value = False
        tracked = {"AA:AA": {ControllerInfoKey.NAME: "P1"}}
        loop = DiscoveryLoop(
            backend=mux,
            tracked_controllers=tracked,
            controller_states={},
            button_detector=button_detector,
            feedback_manager=create_autospec(FeedbackManager, instance=True),
            monitoring=create_autospec(ControllerMonitoring, instance=True),
            rescan_timer=rescan_timer,
            paired_serials=[],
            base_colors={},
            event_publisher=create_autospec(EventPublisher, instance=True),
        )
        loop.backend_initialized = True

        for routed_to in (a1, a2):
            with patch.object(mux, "_resolve_adapter_for_serial", return_value=(routed_to, "targeted")):
                await loop._check_for_new_controllers()
            assert mux._serial_to_adapter["AA:AA"] is routed_to

        button_detector.publish_connection_event.assert_not_called()
        assert set(tracked) == {"AA:AA"}
        assert loop.drain_disconnects() == []


def _specced_adapter(serials):
    adapter = create_autospec(ControllerIOAdapter, instance=True)
    adapter.adapter_type = "mock"
    adapter.discover.return_value = serials
    adapter.open.return_value = True
    adapter.set_output.return_value = True
    adapter.get_adapter_for_serial.return_value = None
    return adapter


class TestLedBatchSendsTheDesiredOutputState:
    """#2355: the 20 Hz batch sends colour + rumble on change and retries a failed write.

    Before, the batch compared only the colour (a rumble change or stop waited for
    the 2 s keep-alive) and marked a colour as sent even when the write failed.
    """

    SERIAL = "AA:AA"

    def _mux_with_colour_sent(self):
        adapter = _specced_adapter([self.SERIAL])
        mux = MultiplexerBackend(adapters=[adapter])
        mux.get_connected_controllers()
        mux._led_colors[self.SERIAL] = (255, 0, 0)
        assert mux.update_all_leds() == 1  # colour sent, keep-alive not due
        adapter.set_output.reset_mock()
        return mux, adapter

    @pytest.mark.uc("UC-002", br=["BR-005"])
    @pytest.mark.asyncio
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    async def test_a_rumble_change_skipped_by_a_busy_lock_goes_out_on_the_next_tick(self, _metrics):
        mux, adapter = self._mux_with_colour_sent()

        with mux._led_lock:  # a batch or discovery swap is in progress
            assert await mux.set_rumble(self.SERIAL, 200) is True
        adapter.set_output.assert_not_called()

        mux.update_all_leds()

        adapter.set_output.assert_called_once_with(self.SERIAL, 255, 0, 0, 200)

    @pytest.mark.uc("UC-002", br=["BR-005"])
    @pytest.mark.asyncio
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    async def test_a_rumble_stop_skipped_by_a_busy_lock_goes_out_on_the_next_tick(self, _metrics):
        mux, adapter = self._mux_with_colour_sent()
        await mux.set_rumble(self.SERIAL, 200)
        adapter.set_output.reset_mock()

        with mux._led_lock:
            await mux.set_rumble(self.SERIAL, 0)

        mux.update_all_leds()

        adapter.set_output.assert_called_once_with(self.SERIAL, 255, 0, 0, 0)

    @pytest.mark.uc("UC-002", br=["BR-005"])
    @pytest.mark.asyncio
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    async def test_a_failed_direct_rumble_write_is_retried_on_the_next_tick(self, _metrics):
        mux, adapter = self._mux_with_colour_sent()
        await mux.set_rumble(self.SERIAL, 200)
        adapter.set_output.return_value = False  # stream reconnecting / queue full

        assert await mux.set_rumble(self.SERIAL, 0) is False

        adapter.set_output.reset_mock()
        adapter.set_output.return_value = True
        mux.update_all_leds()

        adapter.set_output.assert_called_once_with(self.SERIAL, 255, 0, 0, 0)

    @pytest.mark.uc("UC-002", br=["BR-005"])
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_a_failed_batch_write_is_retried_on_the_next_tick(self, _metrics):
        mux, adapter = self._mux_with_colour_sent()
        mux._led_colors[self.SERIAL] = (0, 0, 255)
        adapter.set_output.return_value = False

        mux.update_all_leds()
        assert mux._last_sent_color[self.SERIAL] == (255, 0, 0)  # not marked as sent

        adapter.set_output.reset_mock()
        adapter.set_output.return_value = True
        mux.update_all_leds()

        adapter.set_output.assert_called_once_with(self.SERIAL, 0, 0, 255, 0)
        assert mux._last_sent_color[self.SERIAL] == (0, 0, 255)

    @pytest.mark.uc("UC-002", br=["BR-005"])
    @pytest.mark.asyncio
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    async def test_an_unchanged_state_is_not_resent_before_the_keepalive(self, _metrics):
        mux, adapter = self._mux_with_colour_sent()
        await mux.set_rumble(self.SERIAL, 200)  # direct write succeeds
        adapter.set_output.reset_mock()

        assert mux.update_all_leds() == 0

        adapter.set_output.assert_not_called()

    @pytest.mark.uc("UC-002", br=["BR-005"])
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_a_persistent_failure_is_counted_once_per_keepalive_interval(self, _metrics):
        """Retries run every tick; the health counter keeps the old once-per-2 s cadence."""
        mux, adapter = self._mux_with_colour_sent()
        mux._led_colors[self.SERIAL] = (0, 0, 255)
        adapter.set_output.return_value = False

        for _ in range(5):
            mux.update_all_leds()

        assert adapter.set_output.call_count == 5
        assert mux._led_failures[self.SERIAL] == 1

        mux._led_failures_counted[self.SERIAL] = 0.0
        mux.update_all_leds()
        assert mux._led_failures[self.SERIAL] == 2

    @pytest.mark.uc("UC-002", br=["BR-005"])
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_a_changing_output_during_an_outage_is_counted_once_per_interval(self, _metrics):
        mux, adapter = self._mux_with_colour_sent()
        adapter.set_output.return_value = False

        for level in range(1, 13):  # a shield swell: a new output every tick
            mux._rumble[self.SERIAL] = level
            mux.update_all_leds()

        assert adapter.set_output.call_count == 12
        assert mux._led_failures[self.SERIAL] == 1

        mux._led_failures_counted[self.SERIAL] = 0.0
        mux._rumble[self.SERIAL] = 99
        mux.update_all_leds()
        assert mux._led_failures[self.SERIAL] == 2

    @pytest.mark.uc("UC-002", br=["BR-005"])
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    def test_a_raising_write_is_counted_once_per_interval(self, _metrics):
        mux, adapter = self._mux_with_colour_sent()
        mux._led_colors[self.SERIAL] = (0, 0, 255)
        adapter.set_output.side_effect = OSError("boom")

        for _ in range(5):
            mux.update_all_leds()

        assert adapter.set_output.call_count == 5
        assert mux._led_failures[self.SERIAL] == 1

        mux._led_failures_counted[self.SERIAL] = 0.0
        mux.update_all_leds()
        assert mux._led_failures[self.SERIAL] == 2


class TestRumbleWithoutAColour:
    """#2365: rumble is a desired output of its own; direct-write failures are counted."""

    SERIAL = "AA:AA"

    def _mux(self):
        adapter = _specced_adapter([self.SERIAL])
        mux = MultiplexerBackend(adapters=[adapter])
        mux.get_connected_controllers()
        return mux, adapter

    @pytest.mark.uc("UC-002", br=["BR-005"])
    @pytest.mark.asyncio
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    async def test_a_failed_rumble_on_a_serial_without_a_colour_is_retried(self, _metrics):
        mux, adapter = self._mux()
        adapter.set_output.return_value = False
        assert await mux.set_rumble(self.SERIAL, 200) is False
        adapter.set_output.reset_mock()
        adapter.set_output.return_value = True

        assert mux.update_all_leds() == 1

        adapter.set_output.assert_called_once_with(self.SERIAL, 0, 0, 0, 200)

    @pytest.mark.uc("UC-002", br=["BR-005"])
    @pytest.mark.asyncio
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    async def test_a_rumble_skipped_by_a_busy_lock_without_a_colour_goes_out_next_tick(self, _metrics):
        mux, adapter = self._mux()
        with mux._led_lock:
            await mux.set_rumble(self.SERIAL, 200)
        adapter.set_output.assert_not_called()

        mux.update_all_leds()

        adapter.set_output.assert_called_once_with(self.SERIAL, 0, 0, 0, 200)

    @pytest.mark.uc("UC-002", br=["BR-005"])
    @pytest.mark.asyncio
    @patch("services.controller_manager.multiplexer.multiplexer_backend.metrics")
    async def test_direct_write_failures_count_once_per_interval(self, _metrics):
        mux, adapter = self._mux()
        adapter.set_output.return_value = False

        await mux.set_rumble(self.SERIAL, 200)
        await mux.set_led_color(self.SERIAL, 1, 2, 3)
        await mux.set_rumble(self.SERIAL, 100)

        assert mux._led_failures[self.SERIAL] == 1

        mux._led_failures_counted[self.SERIAL] = 0.0
        await mux.set_led_color(self.SERIAL, 4, 5, 6)
        assert mux._led_failures[self.SERIAL] == 2
