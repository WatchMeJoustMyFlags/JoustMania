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
from services.controller_manager.multiplexer import hid_stream
from services.controller_manager.multiplexer.python_hid_adapter import PythonHidAdapter
from services.controller_manager.multiplexer.rust_adapter import RustServiceAdapter

CANCEL_BOUND_S = 2.0  # the server must see the cancel well inside this
GATE_TIMEOUT_S = 5.0  # every gated fake gives up after this, so a mutation fails instead of hanging
HID_SERIAL = "AA:BB:CC:DD:EE:01"


class FakeHidService(psmove_hid_pb2_grpc.ControllerIOServiceServicer):
    """Serves StreamIO (``frames`` first, then nothing) and records when each stream ends."""

    def __init__(self):
        self.lock = threading.Lock()
        self.streams: list[dict] = []  # one entry per StreamIO call: started / ended events
        self.stream_started = threading.Semaphore(0)
        self.frames: list = []  # sent at the start of every stream

    def StreamIO(self, request_iterator, context):  # noqa: N802 (gRPC method name)
        record = {"ended": threading.Event()}
        context.add_callback(record["ended"].set)  # runs when the RPC terminates (cancel included)
        with self.lock:
            self.streams.append(record)
        self.stream_started.release()
        yield from self.frames
        # Hold the stream open like a quiet HID service (no controllers): no data, no end.
        while context.is_active():
            record["ended"].wait(timeout=0.05)

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


@pytest.mark.uc("UC-002", flow="A7")
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_reader_that_dies_on_a_bad_frame_cancels_its_call(adapter, hid_server, monkeypatch):
    """A non-RpcError in the reader (a frame it cannot convert) still cancels the call on the server."""
    service, _ = hid_server
    service.frames = [psmove_hid_pb2.SensorData(serial=HID_SERIAL)]

    def broken_conversion(_data):
        raise ValueError("bad frame")

    monkeypatch.setattr(hid_stream, "sensor_data_to_dict", broken_conversion)
    stream = _start_stream(adapter, service)
    reader = adapter._stream_thread

    reader.join(timeout=CANCEL_BOUND_S)
    assert not reader.is_alive()
    assert stream["ended"].wait(timeout=CANCEL_BOUND_S), "server still serves the dead reader's stream"
    assert adapter._stream_running is False
    assert adapter._stream_call is None


class GatedCall:
    """A StreamIO call under test control: blocks in ``__next__`` until released or cancelled."""

    def __init__(self, frame=None, cancel_ends_it=True):
        self.frame = frame  # returned once on release, then the call ends
        self.cancel_ends_it = cancel_ends_it
        self.reading = threading.Event()  # the reader is waiting for the next frame
        self.release = threading.Event()
        self.cancelled = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        self.reading.set()
        wake = self.cancelled if self.cancel_ends_it else self.release
        wake.wait(timeout=GATE_TIMEOUT_S)
        if self.release.is_set() and self.frame is not None:
            frame, self.frame = self.frame, None
            return frame
        raise StopIteration

    def cancel(self) -> bool:
        self.cancelled.set()
        return True


class GatedStub:
    """Hands out the given calls, one per StreamIO; ``open_gate`` holds the open of each call."""

    def __init__(self, *calls, open_gate=None):
        self.calls = list(calls)
        self.open_gate = open_gate
        self.opening = threading.Event()

    def StreamIO(self, _command_iterator):  # noqa: N802 (gRPC method name)
        self.opening.set()
        if self.open_gate is not None:
            assert self.open_gate.wait(timeout=GATE_TIMEOUT_S)
        return self.calls.pop(0)


@pytest.fixture
def short_joins(monkeypatch):
    """Stop / reopen give up on a held reader quickly, as they do on a wedged one."""
    monkeypatch.setattr(hid_stream, "STOP_JOIN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(hid_stream, "REOPEN_JOIN_TIMEOUT_S", 0.05)


@pytest.mark.uc("UC-002", flow="A7")
@pytest.mark.usefixtures("short_joins")
def test_late_old_reader_leaves_the_new_stream_alone(adapter):
    """An old reader that outlives stop + reopen neither stops the new stream nor delivers its frame."""
    old_call = GatedCall(frame=psmove_hid_pb2.SensorData(serial=HID_SERIAL), cancel_ends_it=False)
    new_call = GatedCall()
    adapter._stub = GatedStub(old_call, new_call)

    adapter._ensure_stream()
    old_reader = adapter._stream_thread
    assert old_call.reading.wait(timeout=GATE_TIMEOUT_S)
    adapter._stop_stream()  # cancels old_call, but the reader is still inside __next__
    assert old_reader.is_alive()
    adapter._ensure_stream()
    assert new_call.reading.wait(timeout=GATE_TIMEOUT_S)

    old_call.release.set()  # the old reader now gets its frame, after the reopen
    old_reader.join(timeout=GATE_TIMEOUT_S)

    assert not old_reader.is_alive()
    assert adapter._stream_running is True
    assert adapter._stream_call is new_call
    assert not new_call.cancelled.is_set()
    assert adapter._latest_data == {}  # the stale frame was dropped
    assert adapter._trigger_latch == set()


@pytest.mark.uc("UC-002", flow="A7")
@pytest.mark.usefixtures("short_joins")
def test_stop_while_the_call_is_opening_cancels_it(adapter):
    """close_all() lands while StreamIO() is still opening: the reader cancels the call it then gets."""
    open_gate = threading.Event()
    call = GatedCall()
    adapter._stub = GatedStub(call, open_gate=open_gate)

    adapter._ensure_stream()
    reader = adapter._stream_thread
    assert adapter._stub.opening.wait(timeout=GATE_TIMEOUT_S)
    adapter._stop_stream()  # nothing to cancel yet: the call is not open
    assert not call.cancelled.is_set()

    open_gate.set()
    reader.join(timeout=1.0)  # well under GATE_TIMEOUT_S: a reader stuck in the call fails here

    assert not reader.is_alive()
    assert call.cancelled.is_set()
    assert adapter._stream_running is False
    assert adapter._stream_call is None
