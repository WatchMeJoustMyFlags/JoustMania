"""A closed HID adapter cancels its StreamIO call (#2266).

Since grpcio 1.83 a dropped call is no longer cancelled: before #2266,
``close_all()`` only cleared ``_stream_running``, so rust-hid / python-hid kept
serving the closed adapter's stream until the channel closed. These tests run a
real in-process gRPC server (the HID service's side) and check that it sees the
cancellation, through the adapters' real channel with its interceptors.
"""

import threading
from concurrent import futures

import grpc
import pytest

from proto import psmove_hid_pb2, psmove_hid_pb2_grpc
from services.controller_manager.multiplexer.python_hid_adapter import PythonHidAdapter
from services.controller_manager.multiplexer.rust_adapter import RustServiceAdapter

CANCEL_BOUND_S = 2.0  # the server must see the cancel well inside this


class FakeHidService(psmove_hid_pb2_grpc.ControllerIOServiceServicer):
    """Serves StreamIO without sending data and records when each stream ends."""

    def __init__(self):
        self.lock = threading.Lock()
        self.streams: list[dict] = []  # one entry per StreamIO call: started / ended events
        self.stream_started = threading.Semaphore(0)

    def StreamIO(self, request_iterator, context):  # noqa: N802 (gRPC method name)
        record = {"ended": threading.Event()}
        context.add_callback(record["ended"].set)  # runs when the RPC terminates (cancel included)
        with self.lock:
            self.streams.append(record)
        self.stream_started.release()
        # Hold the stream open like a quiet HID service (no controllers): no data, no end.
        while context.is_active():
            record["ended"].wait(timeout=0.05)
        return iter(())

    def CloseAll(self, request, context):  # noqa: N802 (gRPC method name)
        return psmove_hid_pb2.CloseAllResponse()

    def live_streams(self) -> int:
        with self.lock:
            return sum(1 for s in self.streams if not s["ended"].is_set())


@pytest.fixture
def hid_server():
    service = FakeHidService()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    psmove_hid_pb2_grpc.add_ControllerIOServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    yield service, port
    server.stop(grace=0)


@pytest.fixture(params=[RustServiceAdapter, PythonHidAdapter], ids=["rust", "python_hid"])
def adapter(request, hid_server, monkeypatch):
    service, port = hid_server
    prefix = "RUST_HID" if request.param is RustServiceAdapter else "PYTHON_HID"
    monkeypatch.setenv(f"{prefix}_HOST", "127.0.0.1")
    monkeypatch.setenv(f"{prefix}_PORT", str(port))
    instance = request.param()
    yield instance
    instance._stop_stream()


def _start_stream(adapter, service) -> dict:
    adapter._ensure_stream()
    assert service.stream_started.acquire(timeout=CANCEL_BOUND_S), "StreamIO never reached the server"
    with service.lock:
        return service.streams[-1]


@pytest.mark.uc("UC-002", flow="A7")
def test_close_all_cancels_the_stream_on_the_server(adapter, hid_server):
    """close_all(): the HID service sees the StreamIO call end within the bound."""
    service, _ = hid_server
    stream = _start_stream(adapter, service)

    adapter.close_all()

    assert stream["ended"].wait(timeout=CANCEL_BOUND_S), "server still serves the closed adapter's stream"
    assert adapter._stream_running is False
    assert adapter._stream_thread is None
    adapter.close_all()  # idempotent: nothing left to cancel


@pytest.mark.uc("UC-002", flow="A7")
def test_reopen_after_close_all_leaves_one_live_stream(adapter, hid_server):
    """A7 step 2: the next discovery pass reopens the stream; the old one is gone, the new one stays up."""
    service, _ = hid_server
    first = _start_stream(adapter, service)
    adapter.close_all()

    second = _start_stream(adapter, service)

    assert first["ended"].wait(timeout=CANCEL_BOUND_S)
    assert not second["ended"].wait(timeout=0.3)
    assert service.live_streams() == 1
    # The old reader thread ended without marking the new stream as stopped.
    assert adapter._stream_running is True
    assert adapter._stream_thread is not None and adapter._stream_thread.is_alive()
