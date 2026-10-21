"""The HID adapters' sync channels count client errors in grpc_client_errors_total (#1426)."""

from concurrent import futures
from unittest.mock import patch

import grpc
import pytest

from services.controller_manager.multiplexer.python_hid_adapter import PythonHidAdapter
from services.controller_manager.multiplexer.rust_adapter import RustServiceAdapter


def _discover_unavailable(_request, context):
    context.abort(grpc.StatusCode.UNAVAILABLE, "hid service down")


@pytest.fixture
def failing_hid_port():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    handler = grpc.method_handlers_generic_handler(
        "joustmania.psmove_hid.ControllerIOService",
        {"Discover": grpc.unary_unary_rpc_method_handler(_discover_unavailable)},
    )
    server.add_generic_rpc_handlers((handler,))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    yield port
    server.stop(None)


@pytest.mark.uc("UC-035", flow="main", steps=[6])
@pytest.mark.parametrize(
    ("adapter_cls", "env_prefix"),
    [(RustServiceAdapter, "RUST_HID"), (PythonHidAdapter, "PYTHON_HID")],
)
def test_adapter_rpc_error_is_counted(adapter_cls, env_prefix, failing_hid_port, monkeypatch):
    from proto import psmove_hid_pb2

    monkeypatch.setenv(f"{env_prefix}_HOST", "127.0.0.1")
    monkeypatch.setenv(f"{env_prefix}_PORT", str(failing_hid_port))
    adapter = adapter_cls()

    with patch("lib.grpc_metrics.grpc_client_errors_total") as counter:
        with pytest.raises(grpc.RpcError):
            adapter._stub.Discover(psmove_hid_pb2.DiscoverRequest())
        adapter._channel.close()

    counter.labels.assert_called_once_with(service="ControllerIOService", method="Discover", code="UNAVAILABLE")
    counter.labels.return_value.inc.assert_called_once_with()
