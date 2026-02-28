"""
Unit tests for ChaosAdapter — fault injection decorator for ControllerIOAdapters.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from lib.controller_constants import AxisKey, StateKey
from services.controller_manager.multiplexer.chaos_adapter import ChaosAdapter
from services.controller_manager.multiplexer.mock_adapter import MockAdapter


def _make_inner() -> MockAdapter:
    """Create a MockAdapter with one controller for testing."""
    adapter = MockAdapter(num_controllers=0)
    adapter.add_controller("SERIAL001")
    return adapter


def _make_chaos(inner: MockAdapter, fault: str = "none") -> ChaosAdapter:
    """Create a ChaosAdapter with a fixed fault type."""
    chaos = ChaosAdapter(inner)
    # Pre-populate fault map so _evaluate_flag is not called
    chaos._fault_map["SERIAL001"] = fault
    chaos._known_serials.add("SERIAL001")
    return chaos


class TestChaosAdapterPassthrough:
    """With fault='none', all calls delegate to inner adapter unchanged."""

    def test_adapter_type_delegates(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "none")
        assert chaos.adapter_type == "mock"

    def test_discover_returns_all_serials(self):
        inner = _make_inner()
        chaos = ChaosAdapter(inner)
        with patch.object(chaos, "_evaluate_flag", return_value="none"):
            serials = chaos.discover()
        assert "SERIAL001" in serials

    def test_discover_passes_verify_only(self):
        inner = _make_inner()
        chaos = ChaosAdapter(inner)
        mock_discover = MagicMock(return_value=["SERIAL001"])
        inner.discover = mock_discover
        with patch.object(chaos, "_evaluate_flag", return_value="none"):
            chaos.discover(force=True, verify_only=True)
        mock_discover.assert_called_with(force=True, verify_only=True)

    def test_open_delegates(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "none")
        assert chaos.open("SERIAL001") is True

    def test_poll_returns_data(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "none")
        result = chaos.poll("SERIAL001")
        assert result is not None
        assert StateKey.ACCEL in result

    def test_set_output_delegates(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "none")
        assert chaos.set_output("SERIAL001", 255, 0, 0, 0) is True

    def test_close_delegates_and_cleans_up(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "none")
        chaos.close("SERIAL001")
        assert "SERIAL001" not in chaos._known_serials
        assert "SERIAL001" not in chaos._fault_map

    def test_close_all_clears_state(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "none")
        chaos.close_all()
        assert len(chaos._known_serials) == 0
        assert len(chaos._fault_map) == 0


class TestChaosAdapterPollDrop:
    """With fault='poll_drop', poll() returns None ~30% of the time."""

    def test_poll_drop_returns_none_sometimes(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "poll_drop")

        none_count = 0
        trials = 1000
        with patch("services.controller_manager.multiplexer.chaos_adapter.random") as mock_random:
            # Simulate alternating below and above 0.3 threshold
            mock_random.random.side_effect = [0.1 if i % 3 == 0 else 0.5 for i in range(trials)]
            mock_random.gauss = MagicMock()  # Not used for poll_drop

            for _ in range(trials):
                result = chaos.poll("SERIAL001")
                if result is None:
                    none_count += 1

        # ~33% should be None (every 3rd call has random() < 0.3)
        assert none_count > 0
        assert none_count < trials  # Not all are dropped

    def test_poll_drop_increments_metric(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "poll_drop")

        with (
            patch("services.controller_manager.multiplexer.chaos_adapter.random") as mock_random,
            patch("services.controller_manager.multiplexer.chaos_adapter.metrics") as mock_metrics,
        ):
            mock_random.random.return_value = 0.1  # < 0.3, will drop
            chaos.poll("SERIAL001")
            mock_metrics.chaos_faults_injected_total.labels.assert_called_with(fault="poll_drop", serial="SERIAL001")


class TestChaosAdapterAccelSpike:
    """With fault='accel_spike', accel data gets corrupted ~5% of the time."""

    def test_accel_spike_injects_high_values(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "accel_spike")

        with patch("services.controller_manager.multiplexer.chaos_adapter.random") as mock_random:
            mock_random.random.return_value = 0.01  # < 0.05, will spike
            mock_random.gauss.return_value = 5.0

            result = chaos.poll("SERIAL001")

        assert result is not None
        assert result[StateKey.ACCEL][AxisKey.X] == 5.0
        assert result[StateKey.ACCEL][AxisKey.Y] == 5.0
        assert result[StateKey.ACCEL][AxisKey.Z] == 5.0

    def test_accel_spike_passes_through_when_not_triggered(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "accel_spike")

        with patch("services.controller_manager.multiplexer.chaos_adapter.random") as mock_random:
            mock_random.random.return_value = 0.5  # > 0.05, no spike
            result = chaos.poll("SERIAL001")

        assert result is not None
        # Accel should be normal mock values (near 0, 0, 1)
        assert abs(result[StateKey.ACCEL][AxisKey.Z]) < 5.0

    def test_accel_spike_increments_metric(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "accel_spike")

        with (
            patch("services.controller_manager.multiplexer.chaos_adapter.random") as mock_random,
            patch("services.controller_manager.multiplexer.chaos_adapter.metrics") as mock_metrics,
        ):
            mock_random.random.return_value = 0.01  # < 0.05
            mock_random.gauss.return_value = 3.0
            chaos.poll("SERIAL001")
            mock_metrics.chaos_faults_injected_total.labels.assert_called_with(fault="accel_spike", serial="SERIAL001")


class TestChaosAdapterLedFailure:
    """With fault='led_failure', set_output returns False ~50% of the time."""

    def test_led_failure_returns_false_sometimes(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "led_failure")

        with patch("services.controller_manager.multiplexer.chaos_adapter.random") as mock_random:
            mock_random.random.return_value = 0.3  # < 0.5, will fail
            result = chaos.set_output("SERIAL001", 255, 0, 0, 0)
        assert result is False

    def test_led_failure_passes_through_sometimes(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "led_failure")

        with patch("services.controller_manager.multiplexer.chaos_adapter.random") as mock_random:
            mock_random.random.return_value = 0.7  # > 0.5, passes through
            result = chaos.set_output("SERIAL001", 255, 0, 0, 0)
        assert result is True

    def test_led_failure_increments_metric(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "led_failure")

        with (
            patch("services.controller_manager.multiplexer.chaos_adapter.random") as mock_random,
            patch("services.controller_manager.multiplexer.chaos_adapter.metrics") as mock_metrics,
        ):
            mock_random.random.return_value = 0.3  # < 0.5
            chaos.set_output("SERIAL001", 255, 0, 0, 0)
            mock_metrics.chaos_faults_injected_total.labels.assert_called_with(fault="led_failure", serial="SERIAL001")

    def test_led_failure_poll_unaffected(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "led_failure")
        result = chaos.poll("SERIAL001")
        assert result is not None


class TestChaosAdapterDisconnect:
    """With fault='disconnect', controller is fully disconnected."""

    def test_poll_returns_none(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "disconnect")
        assert chaos.poll("SERIAL001") is None

    def test_set_output_returns_false(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "disconnect")
        assert chaos.set_output("SERIAL001", 255, 0, 0, 0) is False

    def test_discover_excludes_serial(self):
        inner = _make_inner()
        chaos = ChaosAdapter(inner)
        # Set up the fault map directly
        chaos._fault_map["SERIAL001"] = "disconnect"
        chaos._known_serials.add("SERIAL001")
        with patch.object(chaos, "_evaluate_flag", return_value="disconnect"):
            serials = chaos.discover()
        assert "SERIAL001" not in serials

    def test_disconnect_increments_metric_on_set_output(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "disconnect")

        with patch("services.controller_manager.multiplexer.chaos_adapter.metrics") as mock_metrics:
            chaos.set_output("SERIAL001", 255, 0, 0, 0)
            mock_metrics.chaos_faults_injected_total.labels.assert_called_with(fault="disconnect", serial="SERIAL001")


class TestChaosAdapterFlagListener:
    """Verify _on_flag_changed re-evaluates all known serials."""

    def test_on_flag_changed_updates_fault_map(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "none")

        # Simulate flag change to poll_drop
        with patch.object(chaos, "_evaluate_flag", return_value="poll_drop"):
            chaos._on_flag_changed(None)

        assert chaos._fault_map["SERIAL001"] == "poll_drop"

    def test_on_flag_changed_updates_all_serials(self):
        inner = MockAdapter(num_controllers=0)
        inner.add_controller("S1")
        inner.add_controller("S2")
        inner.add_controller("S3")

        chaos = ChaosAdapter(inner)
        chaos._known_serials = {"S1", "S2", "S3"}
        chaos._fault_map = {"S1": "none", "S2": "none", "S3": "none"}

        with patch.object(chaos, "_evaluate_flag", return_value="accel_spike"):
            chaos._on_flag_changed(None)

        assert all(v == "accel_spike" for v in chaos._fault_map.values())

    def test_ensure_listener_registers_once(self):
        inner = _make_inner()
        chaos = ChaosAdapter(inner)

        mock_client = MagicMock()
        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            chaos._ensure_listener()
            chaos._ensure_listener()  # Second call should be no-op

        mock_client.add_handler.assert_called_once()
        assert chaos._listener_registered is True


class TestChaosAdapterCleanup:
    """Verify close/close_all clean up fault_map and known_serials."""

    def test_close_removes_serial(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "poll_drop")

        chaos.close("SERIAL001")
        assert "SERIAL001" not in chaos._fault_map
        assert "SERIAL001" not in chaos._known_serials

    def test_close_all_clears_everything(self):
        inner = MockAdapter(num_controllers=0)
        inner.add_controller("S1")
        inner.add_controller("S2")

        chaos = ChaosAdapter(inner)
        chaos._known_serials = {"S1", "S2"}
        chaos._fault_map = {"S1": "poll_drop", "S2": "disconnect"}

        chaos.close_all()
        assert len(chaos._known_serials) == 0
        assert len(chaos._fault_map) == 0


class TestChaosAdapterGetFault:
    """Verify _get_fault lazy evaluation for unknown serials."""

    def test_get_fault_evaluates_unknown_serial(self):
        inner = _make_inner()
        chaos = ChaosAdapter(inner)

        with patch.object(chaos, "_evaluate_flag", return_value="led_failure") as mock_eval:
            fault = chaos._get_fault("NEW_SERIAL")

        assert fault == "led_failure"
        assert "NEW_SERIAL" in chaos._known_serials
        assert chaos._fault_map["NEW_SERIAL"] == "led_failure"
        mock_eval.assert_called_once_with("NEW_SERIAL")

    def test_get_fault_uses_cached_value(self):
        inner = _make_inner()
        chaos = _make_chaos(inner, "poll_drop")

        with patch.object(chaos, "_evaluate_flag") as mock_eval:
            fault = chaos._get_fault("SERIAL001")

        assert fault == "poll_drop"
        mock_eval.assert_not_called()


class TestChaosAdapterGetattr:
    """Verify __getattr__ delegates to inner adapter for extra methods."""

    def test_delegates_add_controller(self):
        inner = _make_inner()
        chaos = ChaosAdapter(inner)
        serial = chaos.add_controller("NEW001")
        assert serial == "NEW001"
        assert "NEW001" in inner.controllers

    def test_delegates_remove_controller(self):
        inner = _make_inner()
        chaos = ChaosAdapter(inner)
        assert chaos.remove_controller("SERIAL001") is True
        assert "SERIAL001" not in inner.controllers
