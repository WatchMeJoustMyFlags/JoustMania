"""
Unit tests for PsMoveAdapter — thin I/O adapter using the psmoveapi C library.

Since the real psmove library is not available in the test environment,
we mock the entire psmove module before importing PsMoveAdapter.
"""

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

# Create a mock psmove module and install it in sys.modules BEFORE importing
# PsMoveAdapter, since the module-level import of psmove happens at load time.
mock_psmove = ModuleType("psmove")
mock_psmove.count_connected = MagicMock(return_value=0)
mock_psmove.PSMove = MagicMock()
mock_psmove.Frame_SecondHalf = 1

# Button constants
mock_psmove.Btn_MOVE = 1 << 0
mock_psmove.Btn_T = 1 << 1
mock_psmove.Btn_PS = 1 << 2
mock_psmove.Btn_CROSS = 1 << 3
mock_psmove.Btn_CIRCLE = 1 << 4
mock_psmove.Btn_SQUARE = 1 << 5
mock_psmove.Btn_TRIANGLE = 1 << 6
mock_psmove.Btn_SELECT = 1 << 7
mock_psmove.Btn_START = 1 << 8

# Battery constants
mock_psmove.Batt_CHARGING = 0xEE
mock_psmove.Batt_CHARGING_DONE = 0xEF
mock_psmove.Batt_MAX = 5
mock_psmove.Batt_80Percent = 4
mock_psmove.Batt_60Percent = 3
mock_psmove.Batt_40Percent = 2
mock_psmove.Batt_20Percent = 1
mock_psmove.Batt_MIN = 0

sys.modules["psmove"] = mock_psmove

from lib.controller_constants import AxisKey, ButtonKey, StateKey
from services.controller_manager.multiplexer.psmove_adapter import (
    ACCEL_SCALE,
    PsMoveAdapter,
    _battery_to_percent,
    suppress_stderr,
)


@pytest.fixture(autouse=True)
def _reset_psmove_mocks():
    """Reset mock_psmove.PSMove and count_connected before each test.

    Prevents side_effect / return_value / call_count from leaking between tests.
    """
    mock_psmove.PSMove.reset_mock()
    mock_psmove.PSMove.side_effect = None
    mock_psmove.PSMove.return_value = MagicMock()
    mock_psmove.count_connected.reset_mock()
    mock_psmove.count_connected.return_value = 0


def _make_fake_move(serial="AA:BB:CC:DD:EE:FF"):
    """Create a MagicMock mimicking a psmove.PSMove handle."""
    move = MagicMock()
    move.get_serial.return_value = serial
    move.poll.side_effect = [True, True, False]  # drain two, then stop
    move.get_trigger.return_value = 128
    move.get_buttons.return_value = 0
    move.get_accelerometer_frame.return_value = (0.0, 0.0, 4096.0)
    move.get_gyroscope_frame.return_value = (0.1, 0.2, 0.3)
    move.get_battery.return_value = mock_psmove.Batt_MAX
    move.get_temperature.return_value = 25.0
    return move


class TestBatteryToPercent:
    def test_charging_returns_100(self):
        assert _battery_to_percent(mock_psmove.Batt_CHARGING) == 100

    def test_charging_done_returns_100(self):
        assert _battery_to_percent(mock_psmove.Batt_CHARGING_DONE) == 100

    def test_max_returns_100(self):
        assert _battery_to_percent(mock_psmove.Batt_MAX) == 100

    def test_80_percent(self):
        assert _battery_to_percent(mock_psmove.Batt_80Percent) == 80

    def test_60_percent(self):
        assert _battery_to_percent(mock_psmove.Batt_60Percent) == 60

    def test_40_percent(self):
        assert _battery_to_percent(mock_psmove.Batt_40Percent) == 40

    def test_20_percent(self):
        assert _battery_to_percent(mock_psmove.Batt_20Percent) == 20

    def test_min_returns_0(self):
        assert _battery_to_percent(mock_psmove.Batt_MIN) == 0

    def test_unknown_value_returns_50(self):
        assert _battery_to_percent(0xFF) == 50


class TestSuppressStderr:
    def test_suppress_stderr_context_manager(self):
        """Verify suppress_stderr runs without error and returns control."""
        with suppress_stderr():
            pass  # Should not raise


class TestPsMoveAdapterInit:
    def test_adapter_type(self):
        adapter = PsMoveAdapter()
        assert adapter.adapter_type == "psmove"

    def test_starts_with_empty_handles(self):
        adapter = PsMoveAdapter()
        assert len(adapter._handles) == 0

    def test_initial_controller_count_is_zero(self):
        adapter = PsMoveAdapter()
        assert adapter._last_controller_count == 0


class TestPsMoveAdapterDiscover:
    def test_discover_returns_serials(self):
        adapter = PsMoveAdapter()
        fake_move = _make_fake_move("AABBCCDDEE01")
        mock_psmove.count_connected.return_value = 1
        mock_psmove.PSMove.return_value = fake_move

        serials = adapter.discover()

        assert "AABBCCDDEE01" in serials
        assert len(serials) == 1

    def test_discover_multiple_controllers(self):
        adapter = PsMoveAdapter()
        moves = [
            _make_fake_move("AABBCCDDEE01"),
            _make_fake_move("AA:BB:CC:DD:EE:02"),
        ]
        mock_psmove.count_connected.return_value = 2
        mock_psmove.PSMove.side_effect = moves

        serials = adapter.discover()

        assert len(serials) == 2
        assert "AABBCCDDEE01" in serials
        assert "AABBCCDDEE02" in serials

    def test_discover_skips_no_change_without_force(self):
        adapter = PsMoveAdapter()
        fake_move = _make_fake_move("AABBCCDDEE01")
        mock_psmove.count_connected.return_value = 1
        mock_psmove.PSMove.return_value = fake_move

        first = adapter.discover()
        # Second discover with same count should return cached list
        second = adapter.discover()

        assert first == second
        # PSMove constructor should only be called once (from the first discover)
        assert mock_psmove.PSMove.call_count == 1

    def test_discover_rescans_on_force(self):
        adapter = PsMoveAdapter()
        fake_move = _make_fake_move("AABBCCDDEE01")
        mock_psmove.count_connected.return_value = 1
        mock_psmove.PSMove.return_value = fake_move

        adapter.discover()
        # Reset mock to track new calls
        mock_psmove.PSMove.reset_mock()
        mock_psmove.PSMove.return_value = _make_fake_move("AABBCCDDEE01")
        adapter.discover(force=True)

        mock_psmove.PSMove.assert_called()

    def test_discover_rescans_on_count_change(self):
        adapter = PsMoveAdapter()
        move1 = _make_fake_move("AABBCCDDEE01")
        mock_psmove.count_connected.return_value = 1
        mock_psmove.PSMove.return_value = move1

        adapter.discover()

        # Count changes — triggers rescan
        move2 = _make_fake_move("AA:BB:CC:DD:EE:02")
        mock_psmove.count_connected.return_value = 2
        mock_psmove.PSMove.side_effect = [
            _make_fake_move("AABBCCDDEE01"),
            move2,
        ]

        serials = adapter.discover()
        assert len(serials) == 2

    def test_discover_removes_stale_handles(self):
        adapter = PsMoveAdapter()
        # First scan: 2 controllers
        mock_psmove.count_connected.return_value = 2
        mock_psmove.PSMove.side_effect = [
            _make_fake_move("AABBCCDDEE01"),
            _make_fake_move("AA:BB:CC:DD:EE:02"),
        ]
        adapter.discover()
        assert len(adapter._handles) == 2

        # Second scan: only 1 controller (count changed)
        mock_psmove.count_connected.return_value = 1
        mock_psmove.PSMove.side_effect = [
            _make_fake_move("AABBCCDDEE01"),
        ]
        serials = adapter.discover()

        assert len(serials) == 1
        assert "AABBCCDDEE01" in serials
        assert "AABBCCDDEE02" not in serials

    def test_discover_handles_psmove_returning_none(self):
        adapter = PsMoveAdapter()
        mock_psmove.count_connected.return_value = 1
        mock_psmove.PSMove.return_value = None

        serials = adapter.discover()

        assert serials == []

    def test_discover_handles_no_serial(self):
        adapter = PsMoveAdapter()
        move = _make_fake_move("")
        move.get_serial.return_value = ""
        mock_psmove.count_connected.return_value = 1
        mock_psmove.PSMove.return_value = move

        serials = adapter.discover()

        assert serials == []

    def test_discover_handles_exception_during_probe(self):
        adapter = PsMoveAdapter()
        mock_psmove.count_connected.return_value = 1
        mock_psmove.PSMove.side_effect = RuntimeError("USB error")

        serials = adapter.discover()

        assert serials == []

    def test_discover_retries_failed_indices(self):
        """When a probe fails but count > seen, retries up to 3 times."""
        adapter = PsMoveAdapter()
        mock_psmove.count_connected.return_value = 1

        # First attempt: exception. Second attempt: success.
        mock_psmove.PSMove.side_effect = [
            RuntimeError("USB busy"),
            _make_fake_move("AABBCCDDEE01"),
        ]

        with patch("services.controller_manager.multiplexer.psmove_adapter.time.sleep"):
            serials = adapter.discover()

        assert "AABBCCDDEE01" in serials

    def test_discover_does_not_duplicate_existing_handle(self):
        adapter = PsMoveAdapter()
        mock_psmove.count_connected.return_value = 1
        first_move = _make_fake_move("AABBCCDDEE01")
        mock_psmove.PSMove.return_value = first_move

        adapter.discover()

        # Force rescan, same serial appears again
        second_move = _make_fake_move("AABBCCDDEE01")
        mock_psmove.PSMove.return_value = second_move
        adapter.discover(force=True)

        # Should still use the first handle, not replace it
        assert adapter._handles["AABBCCDDEE01"] is first_move


class TestPsMoveAdapterSerialNormalization:
    def test_discover_normalizes_colon_format(self):
        """Serials from psmoveapi (colon format) are normalized to uppercase no-colon."""
        adapter = PsMoveAdapter()
        fake_move = _make_fake_move("e0:ae:5e:e1:11:ab")
        mock_psmove.count_connected.return_value = 1
        mock_psmove.PSMove.return_value = fake_move

        serials = adapter.discover()

        assert serials == ["E0AE5EE111AB"]
        assert "E0AE5EE111AB" in adapter._handles

    def test_discover_normalizes_mixed_case(self):
        """Mixed-case serials without colons are uppercased."""
        adapter = PsMoveAdapter()
        fake_move = _make_fake_move("aaBBccDDeeFF")
        mock_psmove.count_connected.return_value = 1
        mock_psmove.PSMove.return_value = fake_move

        serials = adapter.discover()

        assert serials == ["AABBCCDDEEFF"]


class TestPsMoveAdapterOpen:
    def test_open_returns_true_for_known_serial(self):
        adapter = PsMoveAdapter()
        adapter._handles["AABBCCDDEE01"] = _make_fake_move()
        assert adapter.open("AABBCCDDEE01") is True

    def test_open_returns_false_for_unknown_serial(self):
        adapter = PsMoveAdapter()
        assert adapter.open("UNKNOWN") is False


class TestPsMoveAdapterPoll:
    def test_poll_returns_state_dict(self):
        adapter = PsMoveAdapter()
        move = _make_fake_move("AABBCCDDEE01")
        move.get_trigger.return_value = 200
        move.get_buttons.return_value = 0
        move.get_accelerometer_frame.return_value = (100.0, 200.0, 4096.0)
        move.get_gyroscope_frame.return_value = (0.5, 1.0, 1.5)
        move.get_battery.return_value = mock_psmove.Batt_80Percent
        move.get_temperature.return_value = 30.5
        adapter._handles["AABBCCDDEE01"] = move

        state = adapter.poll("AABBCCDDEE01")

        assert state is not None
        assert state[StateKey.SERIAL] == "AABBCCDDEE01"
        assert state[StateKey.TRIGGER] == 200
        assert state[StateKey.BATTERY] == 80
        assert state[StateKey.TEMPERATURE] == 30.5

        # Accelerometer values are scaled by ACCEL_SCALE
        assert state[StateKey.ACCEL][AxisKey.X] == 100.0 / ACCEL_SCALE
        assert state[StateKey.ACCEL][AxisKey.Y] == 200.0 / ACCEL_SCALE
        assert state[StateKey.ACCEL][AxisKey.Z] == 4096.0 / ACCEL_SCALE

        # Gyroscope values are passed through unscaled
        assert state[StateKey.GYRO][AxisKey.X] == 0.5
        assert state[StateKey.GYRO][AxisKey.Y] == 1.0
        assert state[StateKey.GYRO][AxisKey.Z] == 1.5

    def test_poll_returns_none_for_unknown_serial(self):
        adapter = PsMoveAdapter()
        assert adapter.poll("UNKNOWN") is None

    def test_poll_button_flags(self):
        adapter = PsMoveAdapter()
        move = _make_fake_move("AABBCCDDEE01")
        # Set MOVE and TRIGGER buttons
        move.get_buttons.return_value = mock_psmove.Btn_MOVE | mock_psmove.Btn_T
        adapter._handles["AABBCCDDEE01"] = move

        state = adapter.poll("AABBCCDDEE01")

        assert state[ButtonKey.MOVE] is True
        assert state[ButtonKey.TRIGGER] is True
        assert state[ButtonKey.PS] is False
        assert state[ButtonKey.CROSS] is False
        assert state[ButtonKey.CIRCLE] is False
        assert state[ButtonKey.SQUARE] is False
        assert state[ButtonKey.TRIANGLE] is False
        assert state[ButtonKey.SELECT] is False
        assert state[ButtonKey.START] is False

    def test_poll_all_buttons_pressed(self):
        adapter = PsMoveAdapter()
        move = _make_fake_move("AABBCCDDEE01")
        all_buttons = (
            mock_psmove.Btn_MOVE
            | mock_psmove.Btn_T
            | mock_psmove.Btn_PS
            | mock_psmove.Btn_CROSS
            | mock_psmove.Btn_CIRCLE
            | mock_psmove.Btn_SQUARE
            | mock_psmove.Btn_TRIANGLE
            | mock_psmove.Btn_SELECT
            | mock_psmove.Btn_START
        )
        move.get_buttons.return_value = all_buttons
        adapter._handles["AABBCCDDEE01"] = move

        state = adapter.poll("AABBCCDDEE01")

        assert state[ButtonKey.MOVE] is True
        assert state[ButtonKey.TRIGGER] is True
        assert state[ButtonKey.PS] is True
        assert state[ButtonKey.CROSS] is True
        assert state[ButtonKey.CIRCLE] is True
        assert state[ButtonKey.SQUARE] is True
        assert state[ButtonKey.TRIANGLE] is True
        assert state[ButtonKey.SELECT] is True
        assert state[ButtonKey.START] is True

    def test_poll_drains_event_queue(self):
        adapter = PsMoveAdapter()
        move = _make_fake_move("AABBCCDDEE01")
        # poll() returns True three times then False (drains 3 queued reports)
        move.poll.side_effect = [True, True, True, False]
        adapter._handles["AABBCCDDEE01"] = move

        adapter.poll("AABBCCDDEE01")

        assert move.poll.call_count == 4

    def test_poll_returns_none_on_exception(self):
        adapter = PsMoveAdapter()
        move = _make_fake_move("AABBCCDDEE01")
        move.poll.side_effect = RuntimeError("Device disconnected")
        adapter._handles["AABBCCDDEE01"] = move

        state = adapter.poll("AABBCCDDEE01")

        assert state is None

    def test_poll_returns_none_on_get_trigger_exception(self):
        adapter = PsMoveAdapter()
        move = _make_fake_move("AABBCCDDEE01")
        move.poll.side_effect = [False]  # drain completes immediately
        move.get_trigger.side_effect = RuntimeError("Read error")
        adapter._handles["AABBCCDDEE01"] = move

        state = adapter.poll("AABBCCDDEE01")

        assert state is None

    def test_poll_accel_scaling(self):
        """Accelerometer raw values are divided by ACCEL_SCALE (4096.0)."""
        adapter = PsMoveAdapter()
        move = _make_fake_move("AABBCCDDEE01")
        move.get_accelerometer_frame.return_value = (4096.0, -4096.0, 8192.0)
        adapter._handles["AABBCCDDEE01"] = move

        state = adapter.poll("AABBCCDDEE01")

        assert abs(state[StateKey.ACCEL][AxisKey.X] - 1.0) < 1e-9
        assert abs(state[StateKey.ACCEL][AxisKey.Y] - (-1.0)) < 1e-9
        assert abs(state[StateKey.ACCEL][AxisKey.Z] - 2.0) < 1e-9


class TestPsMoveAdapterSetOutput:
    def test_set_output_calls_psmove_methods(self):
        adapter = PsMoveAdapter()
        move = _make_fake_move("AABBCCDDEE01")
        adapter._handles["AABBCCDDEE01"] = move

        result = adapter.set_output("AABBCCDDEE01", 255, 128, 64, 200)

        assert result is True
        move.set_leds.assert_called_once_with(255, 128, 64)
        move.set_rumble.assert_called_once_with(200)
        move.update_leds.assert_called_once()

    def test_set_output_returns_false_for_unknown_serial(self):
        adapter = PsMoveAdapter()
        result = adapter.set_output("UNKNOWN", 0, 0, 0, 0)
        assert result is False

    def test_set_output_returns_false_on_exception(self):
        adapter = PsMoveAdapter()
        move = _make_fake_move("AABBCCDDEE01")
        move.set_leds.side_effect = RuntimeError("USB write error")
        adapter._handles["AABBCCDDEE01"] = move

        result = adapter.set_output("AABBCCDDEE01", 255, 0, 0, 0)

        assert result is False

    def test_set_output_zero_values(self):
        adapter = PsMoveAdapter()
        move = _make_fake_move("AABBCCDDEE01")
        adapter._handles["AABBCCDDEE01"] = move

        result = adapter.set_output("AABBCCDDEE01", 0, 0, 0, 0)

        assert result is True
        move.set_leds.assert_called_once_with(0, 0, 0)
        move.set_rumble.assert_called_once_with(0)


class TestPsMoveAdapterClose:
    def test_close_removes_handle(self):
        adapter = PsMoveAdapter()
        adapter._handles["AABBCCDDEE01"] = _make_fake_move()
        adapter._handles["AA:BB:CC:DD:EE:02"] = _make_fake_move()

        adapter.close("AABBCCDDEE01")

        assert "AABBCCDDEE01" not in adapter._handles
        assert "AA:BB:CC:DD:EE:02" in adapter._handles

    def test_close_nonexistent_does_not_raise(self):
        adapter = PsMoveAdapter()
        adapter.close("NONEXISTENT")  # Should not raise

    def test_close_all_turns_off_leds_and_clears_handles(self):
        adapter = PsMoveAdapter()
        move1 = _make_fake_move("AABBCCDDEE01")
        move2 = _make_fake_move("AA:BB:CC:DD:EE:02")
        adapter._handles["AABBCCDDEE01"] = move1
        adapter._handles["AA:BB:CC:DD:EE:02"] = move2

        adapter.close_all()

        assert len(adapter._handles) == 0
        move1.set_leds.assert_called_with(0, 0, 0)
        move1.set_rumble.assert_called_with(0)
        move1.update_leds.assert_called()
        move2.set_leds.assert_called_with(0, 0, 0)
        move2.set_rumble.assert_called_with(0)
        move2.update_leds.assert_called()

    def test_close_all_suppresses_exceptions(self):
        adapter = PsMoveAdapter()
        move = _make_fake_move("AABBCCDDEE01")
        move.set_leds.side_effect = RuntimeError("Device gone")
        adapter._handles["AABBCCDDEE01"] = move

        # Should not raise even when set_leds fails
        adapter.close_all()

        assert len(adapter._handles) == 0

    def test_close_all_on_empty_handles(self):
        adapter = PsMoveAdapter()
        adapter.close_all()  # Should not raise
        assert len(adapter._handles) == 0


class TestPsMoveAdapterProbeHelpers:
    def test_probe_controller_indices_returns_seen_and_failed(self):
        adapter = PsMoveAdapter()
        move1 = _make_fake_move("AABBCCDDEE01")
        # Second PSMove call raises to simulate failure
        mock_psmove.PSMove.side_effect = [move1, RuntimeError("USB error")]

        seen, failed = adapter._probe_controller_indices(2)

        assert "AABBCCDDEE01" in seen
        assert 1 in failed

    def test_probe_single_controller_success(self):
        adapter = PsMoveAdapter()
        move = _make_fake_move("AABBCCDDEE01")
        mock_psmove.PSMove.return_value = move

        serial = adapter._probe_single_controller(0, 1)

        assert serial == "AABBCCDDEE01"
        assert "AABBCCDDEE01" in adapter._handles

    def test_probe_single_controller_none_returned(self):
        adapter = PsMoveAdapter()
        mock_psmove.PSMove.return_value = None

        serial = adapter._probe_single_controller(0, 1)

        assert serial is None

    def test_probe_single_controller_empty_serial(self):
        adapter = PsMoveAdapter()
        move = MagicMock()
        move.get_serial.return_value = ""
        mock_psmove.PSMove.return_value = move

        serial = adapter._probe_single_controller(0, 1)

        assert serial is None

    def test_probe_single_controller_exception(self):
        adapter = PsMoveAdapter()
        mock_psmove.PSMove.side_effect = OSError("Bluetooth error")

        serial = adapter._probe_single_controller(0, 1)

        assert serial is None

    def test_remove_stale_handles(self):
        adapter = PsMoveAdapter()
        adapter._handles["AABBCCDDEE01"] = _make_fake_move()
        adapter._handles["AA:BB:CC:DD:EE:02"] = _make_fake_move()

        adapter._remove_stale_handles(["AABBCCDDEE01"])

        assert "AABBCCDDEE01" in adapter._handles
        assert "AA:BB:CC:DD:EE:02" not in adapter._handles

    def test_remove_stale_handles_none_stale(self):
        adapter = PsMoveAdapter()
        adapter._handles["AABBCCDDEE01"] = _make_fake_move()

        adapter._remove_stale_handles(["AABBCCDDEE01"])

        assert "AABBCCDDEE01" in adapter._handles

    def test_remove_stale_handles_all_stale(self):
        adapter = PsMoveAdapter()
        adapter._handles["AABBCCDDEE01"] = _make_fake_move()
        adapter._handles["AA:BB:CC:DD:EE:02"] = _make_fake_move()

        adapter._remove_stale_handles([])

        assert len(adapter._handles) == 0
