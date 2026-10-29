"""Each HID service adapter subclass keeps its own backend constants (#2352)."""

from unittest.mock import create_autospec, patch

import grpc
import pytest

from services.controller_manager.multiplexer.python_hid_adapter import PythonHidAdapter
from services.controller_manager.multiplexer.rust_adapter import RustServiceAdapter


@pytest.mark.uc("UC-035", br=["BR-009"])
@pytest.mark.parametrize(
    ("adapter_cls", "target", "adapter_type", "thread_name", "peer"),
    [
        (RustServiceAdapter, "localhost:50058", "rust", "rust-adapter-stream", "rust-hid service"),
        (PythonHidAdapter, "localhost:50059", "python", "python-adapter-stream", "python service"),
    ],
    ids=["rust", "python_hid"],
)
def test_backend_defaults(monkeypatch, adapter_cls, target, adapter_type, thread_name, peer):
    for prefix in ("RUST_HID", "PYTHON_HID"):
        monkeypatch.delenv(f"{prefix}_HOST", raising=False)
        monkeypatch.delenv(f"{prefix}_PORT", raising=False)
    insecure_channel = create_autospec(grpc.insecure_channel)
    with (
        patch("grpc.insecure_channel", insecure_channel),
        patch("grpc.intercept_channel"),
    ):
        adapter = adapter_cls()

    assert insecure_channel.call_args.args[0] == target
    assert adapter.adapter_type == adapter_type
    assert adapter._stream_thread_name == thread_name
    assert adapter._stream_peer == peer
