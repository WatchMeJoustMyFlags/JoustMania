"""
Unit tests for extracted BluetoothBackend helper methods.

Tests the private helpers extracted from get_connected_controllers to
reduce cognitive complexity:
- _probe_controller: probes a single psmove index
- _scan_controllers_with_retries: retry loop over all indices
- _remove_stale_controllers: cleanup of disconnected controllers
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))


class MockPSMove:
    """Mock psmove module constants and classes."""

    Batt_MIN = 0x00
    Batt_20Percent = 0x01
    Batt_40Percent = 0x02
    Batt_60Percent = 0x03
    Batt_80Percent = 0x04
    Batt_MAX = 0x05
    Batt_CHARGING = 0xEE
    Batt_CHARGING_DONE = 0xEF

    Btn_MOVE = 0x01
    Btn_T = 0x02
    Btn_PS = 0x04
    Btn_CROSS = 0x08
    Btn_CIRCLE = 0x10
    Btn_SQUARE = 0x20
    Btn_TRIANGLE = 0x40
    Btn_SELECT = 0x80
    Btn_START = 0x100

    Frame_SecondHalf = 1

    @staticmethod
    def count_connected():
        return 0

    class PSMove:
        def __init__(self, index=0):
            self._serial = f"test_serial_{index}"
            self._leds = (0, 0, 0)
            self._rumble = 0

        def get_serial(self):
            return self._serial

        def poll(self):
            return False

        def get_trigger(self):
            return 0

        def get_buttons(self):
            return 0

        def get_accelerometer_frame(self, frame):
            return (0.0, 0.0, 4096.0)

        def get_gyroscope_frame(self, frame):
            return (0.0, 0.0, 0.0)

        def get_battery(self):
            return MockPSMove.Batt_MAX

        def get_temperature(self):
            return 25.0

        def set_leds(self, r, g, b):
            self._leds = (r, g, b)

        def update_leds(self):
            pass

        def set_rumble(self, intensity):
            self._rumble = intensity


class MockControllerState:
    """Minimal mock for ControllerState."""

    pass


def _make_backend():
    """Create a BluetoothBackend instance with all psmove dependencies mocked.

    Returns a backend with empty tracking dicts, ready for testing helpers.
    Uses sys.modules patching to make psmove importable and reloads the module.

    Note: adapter detection moved to initialize() in Phase 3, so __init__
    no longer calls bluetooth.get_hci_dict(). We still mock bluetooth to
    prevent real imports.
    """
    # Mock the bluetooth module
    mock_bluetooth = MagicMock()

    # Mock controller_state module
    mock_cs_module = MagicMock()
    mock_cs_module.ControllerState = MockControllerState

    with patch.dict(sys.modules, {"psmove": MockPSMove}):
        # Force re-import so it picks up mocked psmove
        import services.controller_manager.bluetooth_backend as bb_module

        importlib.reload(bb_module)

        # Now patch the module-level names that were set during import
        with (
            patch.object(bb_module, "bluetooth", mock_bluetooth),
            patch.object(bb_module, "ControllerState", MockControllerState),
        ):
            backend = bb_module.BluetoothBackend()

    return backend


# ---- _probe_controller tests ----


class TestProbeController:
    """Tests for _probe_controller helper."""

    def test_probe_returns_serial_for_valid_controller(self):
        """Probing a valid controller index should return its serial."""
        backend = _make_backend()
        move = MockPSMove.PSMove(0)
        move._serial = "AA:BB:CC:DD:EE:00"

        with (
            patch.dict(sys.modules, {"psmove": MockPSMove}),
            patch("services.controller_manager.bluetooth_backend.psmove", MockPSMove),
            patch("services.controller_manager.bluetooth_backend.ControllerState", MockControllerState),
            patch("services.controller_manager.bluetooth_backend.suppress_stderr"),
            patch.object(MockPSMove, "PSMove", return_value=move),
        ):
            serial = backend._probe_controller(0, 1)

        assert serial == "AABBCCDDEE00"

    def test_probe_registers_new_controller(self):
        """Probing a new controller should register it in controllers dict."""
        backend = _make_backend()
        move = MockPSMove.PSMove(0)
        move._serial = "AA:BB:CC:DD:EE:01"

        with (
            patch.dict(sys.modules, {"psmove": MockPSMove}),
            patch("services.controller_manager.bluetooth_backend.psmove", MockPSMove),
            patch("services.controller_manager.bluetooth_backend.ControllerState", MockControllerState),
            patch("services.controller_manager.bluetooth_backend.suppress_stderr"),
            patch.object(MockPSMove, "PSMove", return_value=move),
        ):
            backend._probe_controller(0, 1)

        assert "AABBCCDDEE01" in backend.controllers
        assert "AABBCCDDEE01" in backend.controller_states

    def test_probe_skips_already_tracked_controller(self):
        """Probing an already-tracked serial should not overwrite the stored handle."""
        backend = _make_backend()
        original_move = MagicMock()
        backend.controllers["AABBCCDDEE02"] = original_move

        new_move = MockPSMove.PSMove(0)
        new_move._serial = "AA:BB:CC:DD:EE:02"

        with (
            patch.dict(sys.modules, {"psmove": MockPSMove}),
            patch("services.controller_manager.bluetooth_backend.psmove", MockPSMove),
            patch("services.controller_manager.bluetooth_backend.ControllerState", MockControllerState),
            patch("services.controller_manager.bluetooth_backend.suppress_stderr"),
            patch.object(MockPSMove, "PSMove", return_value=new_move),
        ):
            serial = backend._probe_controller(0, 1)

        assert serial == "AABBCCDDEE02"
        # Original handle preserved
        assert backend.controllers["AABBCCDDEE02"] is original_move

    def test_probe_returns_none_when_psmove_returns_none(self):
        """Probing should return None when PSMove() returns None."""
        backend = _make_backend()

        with (
            patch.dict(sys.modules, {"psmove": MockPSMove}),
            patch("services.controller_manager.bluetooth_backend.psmove", MockPSMove),
            patch("services.controller_manager.bluetooth_backend.suppress_stderr"),
            patch.object(MockPSMove, "PSMove", return_value=None),
        ):
            serial = backend._probe_controller(0, 1)

        assert serial is None

    def test_probe_returns_none_when_no_serial(self):
        """Probing should return None when controller has no serial."""
        backend = _make_backend()
        move = MockPSMove.PSMove(0)
        move.get_serial = lambda: ""

        with (
            patch.dict(sys.modules, {"psmove": MockPSMove}),
            patch("services.controller_manager.bluetooth_backend.psmove", MockPSMove),
            patch("services.controller_manager.bluetooth_backend.suppress_stderr"),
            patch.object(MockPSMove, "PSMove", return_value=move),
        ):
            serial = backend._probe_controller(0, 1)

        assert serial is None

    def test_probe_returns_none_on_exception(self):
        """Probing should return None when PSMove() raises an exception."""
        backend = _make_backend()

        with (
            patch.dict(sys.modules, {"psmove": MockPSMove}),
            patch("services.controller_manager.bluetooth_backend.psmove", MockPSMove),
            patch("services.controller_manager.bluetooth_backend.suppress_stderr"),
            patch.object(MockPSMove, "PSMove", side_effect=RuntimeError("hardware error")),
        ):
            serial = backend._probe_controller(0, 1)

        assert serial is None


# ---- _scan_controllers_with_retries tests ----


class TestScanControllersWithRetries:
    """Tests for _scan_controllers_with_retries helper."""

    def test_returns_all_serials_on_first_attempt(self):
        """When all controllers probe successfully, should return on first attempt."""
        backend = _make_backend()

        serials = ["SERIAL_A", "SERIAL_B"]
        call_count = [0]

        def mock_probe(move_num, _count):
            call_count[0] += 1
            return serials[move_num]

        backend._probe_controller = mock_probe

        result = backend._scan_controllers_with_retries(2)

        assert result == ["SERIAL_A", "SERIAL_B"]
        assert call_count[0] == 2  # Only one pass, no retries

    def test_retries_on_partial_failure(self):
        """Should retry when some controllers fail to probe."""
        backend = _make_backend()

        attempt_num = [0]

        def mock_probe(move_num, _count):
            if move_num == 1 and attempt_num[0] == 0:
                return None  # Fail on first attempt for second controller
            return f"SERIAL_{move_num}"

        # Wrap to track attempt boundaries
        original_probe = mock_probe

        def tracking_probe(move_num, count):
            result = original_probe(move_num, count)
            if move_num == count - 1:
                attempt_num[0] += 1
            return result

        backend._probe_controller = tracking_probe

        with patch("services.controller_manager.bluetooth_backend.time") as mock_time:
            mock_time.sleep = MagicMock()
            result = backend._scan_controllers_with_retries(2, max_retries=2, retry_delay=0.1)

        assert "SERIAL_0" in result
        assert "SERIAL_1" in result

    def test_stops_retrying_when_all_found(self):
        """Should not retry when all controllers are found."""
        backend = _make_backend()

        probe_calls = [0]

        def mock_probe(move_num, _count):
            probe_calls[0] += 1
            return f"SERIAL_{move_num}"

        backend._probe_controller = mock_probe

        result = backend._scan_controllers_with_retries(3, max_retries=3)

        assert len(result) == 3
        assert probe_calls[0] == 3  # Only one pass

    def test_returns_empty_list_for_zero_count(self):
        """Should return empty list when count is 0."""
        backend = _make_backend()

        probe_calls = [0]

        def mock_probe(_move_num, _count):
            probe_calls[0] += 1

        backend._probe_controller = mock_probe

        result = backend._scan_controllers_with_retries(0)

        assert result == []
        assert probe_calls[0] == 0

    def test_respects_max_retries(self):
        """Should not exceed max_retries attempts."""
        backend = _make_backend()

        probe_calls = [0]

        def always_fail(_move_num, _count):
            probe_calls[0] += 1

        backend._probe_controller = always_fail

        with patch("services.controller_manager.bluetooth_backend.time") as mock_time:
            mock_time.sleep = MagicMock()
            result = backend._scan_controllers_with_retries(1, max_retries=3, retry_delay=0.1)

        assert result == []
        assert probe_calls[0] == 3  # Exactly 3 attempts
        assert mock_time.sleep.call_count == 2  # Sleeps between retries (not after last)


# ---- _remove_stale_controllers tests ----


class TestRemoveStaleControllers:
    """Tests for _remove_stale_controllers helper."""

    def test_removes_stale_controller(self):
        """Controllers not in seen_serials should be removed."""
        backend = _make_backend()
        backend.controllers["STALE_1"] = MagicMock()
        backend.controller_states["STALE_1"] = MockControllerState()
        backend.led_colors["STALE_1"] = (255, 0, 0)
        backend._last_sent_color["STALE_1"] = (255, 0, 0)
        backend._last_led_update["STALE_1"] = 1234567890.0
        backend._effect_active.add("STALE_1")

        backend._remove_stale_controllers([])

        assert "STALE_1" not in backend.controllers
        assert "STALE_1" not in backend.controller_states
        assert "STALE_1" not in backend.led_colors
        assert "STALE_1" not in backend._last_sent_color
        assert "STALE_1" not in backend._last_led_update
        assert "STALE_1" not in backend._effect_active

    def test_keeps_seen_controllers(self):
        """Controllers in seen_serials should not be removed."""
        backend = _make_backend()
        backend.controllers["KEEP_1"] = MagicMock()
        backend.controller_states["KEEP_1"] = MockControllerState()

        backend._remove_stale_controllers(["KEEP_1"])

        assert "KEEP_1" in backend.controllers
        assert "KEEP_1" in backend.controller_states

    def test_mixed_stale_and_seen(self):
        """Only stale controllers should be removed, seen ones kept."""
        backend = _make_backend()
        backend.controllers["KEEP"] = MagicMock()
        backend.controllers["STALE"] = MagicMock()
        backend.controller_states["KEEP"] = MockControllerState()
        backend.controller_states["STALE"] = MockControllerState()

        backend._remove_stale_controllers(["KEEP"])

        assert "KEEP" in backend.controllers
        assert "STALE" not in backend.controllers

    def test_no_stale_controllers(self):
        """When all tracked controllers are seen, nothing should be removed."""
        backend = _make_backend()
        backend.controllers["A"] = MagicMock()
        backend.controllers["B"] = MagicMock()

        backend._remove_stale_controllers(["A", "B"])

        assert len(backend.controllers) == 2

    def test_empty_tracking_is_safe(self):
        """Removing stale from empty tracking should not raise."""
        backend = _make_backend()

        # Should not raise
        backend._remove_stale_controllers(["NONEXISTENT"])

        assert len(backend.controllers) == 0

    def test_removes_stale_without_optional_tracking(self):
        """Stale controller cleanup should be safe when optional tracking is missing."""
        backend = _make_backend()
        backend.controllers["STALE"] = MagicMock()
        # No led_colors, _last_sent_color, _last_led_update, or _effect_active entries

        backend._remove_stale_controllers([])

        assert "STALE" not in backend.controllers

    def test_removes_stale_without_controller_state(self):
        """Stale cleanup should handle missing controller_states entry."""
        backend = _make_backend()
        backend.controllers["STALE"] = MagicMock()
        # Intentionally no controller_states entry

        backend._remove_stale_controllers([])

        assert "STALE" not in backend.controllers
