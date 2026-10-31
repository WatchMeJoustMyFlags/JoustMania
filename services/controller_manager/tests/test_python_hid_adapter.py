"""PythonHidAdapter unary calls carry the rust adapter's deadline (#2140, follow-up of #2125)."""

import queue
import threading
from unittest.mock import MagicMock

import grpc
import pytest

from proto import psmove_hid_pb2
from services.controller_manager.multiplexer.adapter import DiscoveryFailedError
from services.controller_manager.multiplexer.python_hid_adapter import PythonHidAdapter
from services.controller_manager.multiplexer.rust_adapter import RPC_TIMEOUT_S
from services.controller_manager.tests.backend_doubles import autospec_channel
from services.controller_manager.tests.fake_stream_call import FakeStreamCall

SERIAL = "AABBCCDDEEFF"


def _make_adapter() -> PythonHidAdapter:
    """PythonHidAdapter without __init__ (no gRPC channel), stream marked running."""
    adapter = PythonHidAdapter.__new__(PythonHidAdapter)
    adapter._init_stream_state()
    adapter._channel = autospec_channel()
    adapter._stub = MagicMock(spec_set=["Discover", "Open", "Close", "CloseAll", "StreamIO"])
    adapter._command_queue = queue.Queue(maxsize=256)
    adapter._latest_data = {}
    adapter._last_state = {}
    adapter._trigger_latch = set()
    adapter._data_lock = threading.Lock()
    adapter._closed = set()
    adapter._stream_thread = None
    adapter._stream_running = True  # skip the stream thread
    adapter._adapter_names = {}
    return adapter


class TestUnaryCallsCarryADeadline:
    """A wedged python-hid must not freeze the controller manager (#2140)."""

    @pytest.mark.uc("UC-002", flow="main", steps=[3])
    def test_every_unary_call_has_the_deadline(self):
        adapter = _make_adapter()
        adapter._stub.Discover.return_value.controllers = []
        adapter._stub.Open.return_value.success = True

        adapter.discover()
        adapter.open(SERIAL)
        adapter.close(SERIAL)
        adapter.close_all()

        for rpc in (adapter._stub.Discover, adapter._stub.Open, adapter._stub.Close, adapter._stub.CloseAll):
            assert rpc.call_args.kwargs.get("timeout") == RPC_TIMEOUT_S, rpc

    @pytest.mark.uc("UC-002", flow="A1")
    def test_a_timed_out_discover_is_a_failed_pass_not_an_empty_one(self):
        adapter = _make_adapter()
        err = grpc.RpcError()
        err.code = lambda: grpc.StatusCode.DEADLINE_EXCEEDED
        adapter._stub.Discover.side_effect = err

        with pytest.raises(DiscoveryFailedError):
            adapter.discover()

    def test_a_failed_open_reads_as_open_failed(self):
        adapter = _make_adapter()
        adapter._stub.Open.side_effect = grpc.RpcError()

        assert adapter.open(SERIAL) is False

    def test_stream_io_has_no_deadline(self):
        """The long-lived StreamIO stream must not inherit the unary deadline."""
        adapter = _make_adapter()
        adapter._stub.StreamIO.return_value = FakeStreamCall()

        adapter._stream_loop()

        adapter._stub.StreamIO.assert_called_once()
        assert "timeout" not in adapter._stub.StreamIO.call_args.kwargs


class TestAdapterNameOnRediscovery:
    """A rediscovery with an empty adapter name must not keep the old hciN (UC-035 BR-009, #2133)."""

    @pytest.mark.uc("UC-002", flow="A4")
    @pytest.mark.uc("UC-035", br=["BR-009"])
    def test_rediscovery_with_empty_adapter_name_clears_the_old_adapter(self):
        adapter = _make_adapter()
        ctrl = psmove_hid_pb2.DiscoveredController()
        ctrl.serial = SERIAL
        ctrl.product_id = 0x03D5
        ctrl.adapter_name = "hci1"
        response = psmove_hid_pb2.DiscoverResponse(controllers=[ctrl])
        adapter._stub.Discover.return_value = response
        adapter.discover()
        assert adapter.get_adapter_for_serial(SERIAL) == "hci1"

        response.controllers[0].adapter_name = ""
        adapter.discover()

        assert adapter.get_adapter_for_serial(SERIAL) is None
