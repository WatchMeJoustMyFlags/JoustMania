"""Tests for RustServiceAdapter gRPC client."""

import queue
from unittest.mock import MagicMock

import pytest

from lib.controller_constants import AxisKey, ButtonKey, StateKey
from services.controller_manager.multiplexer.rust_adapter import (
    RustServiceAdapter,
    _sensor_data_to_dict,
)


def _make_adapter():
    """Create a RustServiceAdapter bypassing __init__ gRPC imports."""
    adapter = RustServiceAdapter.__new__(RustServiceAdapter)
    adapter._channel = MagicMock()
    adapter._stub = MagicMock()
    adapter._command_queue = queue.Queue(maxsize=256)
    adapter._latest_data = {}
    adapter._data_lock = __import__("threading").Lock()
    adapter._stream_thread = None
    adapter._stream_running = False
    adapter._adapter_names = {}
    return adapter


class TestRustServiceAdapterProperties:
    """Basic adapter properties."""

    def test_adapter_type(self):
        adapter = _make_adapter()
        assert adapter.adapter_type == "rust"


class TestDiscover:
    """Tests for discover() RPC."""

    def test_discover_returns_serials(self):
        adapter = _make_adapter()
        mock_response = MagicMock()
        mock_ctrl1 = MagicMock()
        mock_ctrl1.serial = "AABBCCDDEEFF"
        mock_ctrl1.adapter_name = "hci0"
        mock_ctrl1.product_id = 0x03D5
        mock_ctrl2 = MagicMock()
        mock_ctrl2.serial = "112233445566"
        mock_ctrl2.adapter_name = "hci1"
        mock_ctrl2.product_id = 0x03D5
        mock_response.controllers = [mock_ctrl1, mock_ctrl2]
        adapter._stub.Discover.return_value = mock_response
        adapter._stream_running = True  # Skip stream start

        serials = adapter.discover(force=True)

        assert serials == ["AABBCCDDEEFF", "112233445566"]
        adapter._stub.Discover.assert_called_once()
        call_args = adapter._stub.Discover.call_args[0][0]
        assert call_args.force is True
        assert call_args.verify_only is False

    def test_discover_verify_only(self):
        adapter = _make_adapter()
        mock_response = MagicMock()
        mock_ctrl = MagicMock()
        mock_ctrl.serial = "AABBCCDDEEFF"
        mock_ctrl.adapter_name = "hci0"
        mock_ctrl.product_id = 0x03D5
        mock_response.controllers = [mock_ctrl]
        adapter._stub.Discover.return_value = mock_response
        adapter._stream_running = True

        adapter.discover(verify_only=True)

        call_args = adapter._stub.Discover.call_args[0][0]
        assert call_args.verify_only is True

    def test_discover_rpc_error_returns_empty(self):
        import grpc

        adapter = _make_adapter()
        adapter._stub.Discover.side_effect = grpc.RpcError()
        adapter._stream_running = True

        serials = adapter.discover()

        assert serials == []


class TestGetAdapterForSerial:
    """Tests for get_adapter_for_serial() — adapter affinity cache."""

    def test_returns_cached_adapter_name(self):
        adapter = _make_adapter()
        mock_response = MagicMock()
        mock_ctrl = MagicMock()
        mock_ctrl.serial = "AABBCCDDEEFF"
        mock_ctrl.adapter_name = "hci0"
        mock_ctrl.product_id = 0x03D5
        mock_response.controllers = [mock_ctrl]
        adapter._stub.Discover.return_value = mock_response
        adapter._stream_running = True

        adapter.discover()

        assert adapter.get_adapter_for_serial("AABBCCDDEEFF") == "hci0"

    def test_returns_none_for_unknown(self):
        adapter = _make_adapter()
        assert adapter.get_adapter_for_serial("UNKNOWN") is None

    def test_returns_none_for_empty_adapter_name(self):
        adapter = _make_adapter()
        mock_response = MagicMock()
        mock_ctrl = MagicMock()
        mock_ctrl.serial = "AABBCCDDEEFF"
        mock_ctrl.adapter_name = ""
        mock_ctrl.product_id = 0x03D5
        mock_response.controllers = [mock_ctrl]
        adapter._stub.Discover.return_value = mock_response
        adapter._stream_running = True

        adapter.discover()

        assert adapter.get_adapter_for_serial("AABBCCDDEEFF") is None

    def test_close_clears_adapter_name(self):
        adapter = _make_adapter()
        adapter._adapter_names["AABBCCDDEEFF"] = "hci0"

        adapter.close("AABBCCDDEEFF")

        assert adapter.get_adapter_for_serial("AABBCCDDEEFF") is None

    def test_close_all_clears_adapter_names(self):
        adapter = _make_adapter()
        adapter._adapter_names["SERIAL_A"] = "hci0"
        adapter._adapter_names["SERIAL_B"] = "hci1"

        adapter.close_all()

        assert adapter.get_adapter_for_serial("SERIAL_A") is None
        assert adapter.get_adapter_for_serial("SERIAL_B") is None


class TestOpen:
    """Tests for open() RPC."""

    def test_open_success(self):
        adapter = _make_adapter()
        mock_response = MagicMock()
        mock_response.success = True
        adapter._stub.Open.return_value = mock_response
        adapter._stream_running = True

        assert adapter.open("AABBCCDDEEFF") is True

    def test_open_failure(self):
        adapter = _make_adapter()
        mock_response = MagicMock()
        mock_response.success = False
        adapter._stub.Open.return_value = mock_response
        adapter._stream_running = True

        assert adapter.open("AABBCCDDEEFF") is False

    def test_open_rpc_error(self):
        import grpc

        adapter = _make_adapter()
        adapter._stub.Open.side_effect = grpc.RpcError()
        adapter._stream_running = True

        assert adapter.open("AABBCCDDEEFF") is False


class TestPoll:
    """Tests for poll() — reads from cached stream data."""

    def test_poll_returns_cached_data(self):
        adapter = _make_adapter()
        state = {StateKey.SERIAL: "AABBCCDDEEFF", StateKey.BATTERY: 80}
        adapter._latest_data["AABBCCDDEEFF"] = state

        result = adapter.poll("AABBCCDDEEFF")

        assert result is not None
        assert result[StateKey.SERIAL] == "AABBCCDDEEFF"
        assert result[StateKey.BATTERY] == 80

    def test_poll_consumes_data(self):
        adapter = _make_adapter()
        adapter._latest_data["AABBCCDDEEFF"] = {StateKey.SERIAL: "AABBCCDDEEFF"}

        adapter.poll("AABBCCDDEEFF")
        result = adapter.poll("AABBCCDDEEFF")

        assert result is None

    def test_poll_no_data(self):
        adapter = _make_adapter()
        assert adapter.poll("AABBCCDDEEFF") is None

    def test_poll_per_serial_isolation(self):
        adapter = _make_adapter()
        adapter._latest_data["SERIAL_A"] = {StateKey.SERIAL: "SERIAL_A"}
        adapter._latest_data["SERIAL_B"] = {StateKey.SERIAL: "SERIAL_B"}

        result_a = adapter.poll("SERIAL_A")
        result_b = adapter.poll("SERIAL_B")

        assert result_a[StateKey.SERIAL] == "SERIAL_A"
        assert result_b[StateKey.SERIAL] == "SERIAL_B"


class TestSetOutput:
    """Tests for set_output() — queues IOCommands for the stream."""

    def test_set_output_queues_command(self):
        adapter = _make_adapter()
        adapter._stream_running = True

        result = adapter.set_output("AABBCCDDEEFF", 255, 128, 64, 200)

        assert result is True
        assert not adapter._command_queue.empty()
        cmd = adapter._command_queue.get_nowait()
        assert cmd.serial == "AABBCCDDEEFF"
        assert cmd.r == 255
        assert cmd.g == 128
        assert cmd.b == 64
        assert cmd.rumble == 200

    def test_set_output_stream_not_running(self):
        adapter = _make_adapter()
        adapter._stream_running = False

        result = adapter.set_output("AABBCCDDEEFF", 255, 0, 0, 0)

        assert result is False


class TestClose:
    """Tests for close() and close_all() RPCs."""

    def test_close_calls_rpc(self):
        adapter = _make_adapter()
        adapter._latest_data["AABBCCDDEEFF"] = {StateKey.SERIAL: "AABBCCDDEEFF"}

        adapter.close("AABBCCDDEEFF")

        adapter._stub.Close.assert_called_once()
        assert "AABBCCDDEEFF" not in adapter._latest_data

    def test_close_all_calls_rpc_and_clears_data(self):
        adapter = _make_adapter()
        adapter._latest_data["SERIAL_A"] = {}
        adapter._latest_data["SERIAL_B"] = {}

        adapter.close_all()

        adapter._stub.CloseAll.assert_called_once()
        assert len(adapter._latest_data) == 0


class TestSensorDataConversion:
    """Tests for _sensor_data_to_dict proto -> dict conversion."""

    def test_full_conversion(self):
        data = MagicMock()
        data.serial = "AABBCCDDEEFF"
        data.battery = 80
        data.trigger = 200
        data.temperature = 300
        data.button_move = True
        data.button_trigger = False
        data.button_ps = True
        data.button_cross = False
        data.button_circle = True
        data.button_square = False
        data.button_triangle = True
        data.button_select = False
        data.button_start = True
        data.accel_x = 0.1
        data.accel_y = -0.2
        data.accel_z = 0.98
        data.gyro_x = 10.0
        data.gyro_y = -5.0
        data.gyro_z = 0.0

        result = _sensor_data_to_dict(data)

        assert result[StateKey.SERIAL] == "AABBCCDDEEFF"
        assert result[StateKey.BATTERY] == 80
        assert result[StateKey.TRIGGER] == 200
        assert result[StateKey.TEMPERATURE] == 300
        assert result[ButtonKey.MOVE] is True
        assert result[ButtonKey.TRIGGER] is False
        assert result[ButtonKey.PS] is True
        assert result[ButtonKey.CROSS] is False
        assert result[ButtonKey.CIRCLE] is True
        assert result[ButtonKey.SQUARE] is False
        assert result[ButtonKey.TRIANGLE] is True
        assert result[ButtonKey.SELECT] is False
        assert result[ButtonKey.START] is True
        assert result[StateKey.ACCEL][AxisKey.X] == pytest.approx(0.1)
        assert result[StateKey.ACCEL][AxisKey.Y] == pytest.approx(-0.2)
        assert result[StateKey.ACCEL][AxisKey.Z] == pytest.approx(0.98)
        assert result[StateKey.GYRO][AxisKey.X] == pytest.approx(10.0)
        assert result[StateKey.GYRO][AxisKey.Y] == pytest.approx(-5.0)
        assert result[StateKey.GYRO][AxisKey.Z] == pytest.approx(0.0)
