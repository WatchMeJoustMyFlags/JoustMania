"""A lagging StreamIO reader still gets fresh samples (#2372, NFR-056).

rust-hid keeps one waiting sample per controller (#2391), so its own queue
never grows. What is left between the HID service and the adapter's reader
thread is the HTTP/2 receive window of the adapter's channel: grpc-core grows
it with BDP probing, and everything inside it is sent before the reader asks
for it. With a reader slower than the samples arrive, the window filled with
old samples and input arrived seconds late (measured 2026-11-01 against the
real tonic server: 1.8 s max at a 1 ms/message reader, 6.7 s at 2 ms).

These tests run a real in-process gRPC server that sends a fresh sample each
time gRPC asks for one (like rust-hid's latest-sample slot: a sample is built
when the transport has room for it), through the adapter's real channel, and
slow the adapter's reader down to half the arrival rate.
"""

import statistics
import threading
import time
from concurrent import futures

import grpc
import pytest

from proto import psmove_hid_pb2, psmove_hid_pb2_grpc
from services.controller_manager.multiplexer import hid_stream
from services.controller_manager.multiplexer.python_hid_adapter import PythonHidAdapter
from services.controller_manager.multiplexer.rust_adapter import RustServiceAdapter

SAMPLE_RATE_HZ = 1000  # all controllers together, as rust-hid sends them at full HID rate
CONTROLLERS = 4
READER_DELAY_S = 0.002  # the reader takes 2 ms per sample: half the arrival rate
WARM_UP_S = 1.0
MEASURE_S = 2.0
STALENESS_BOUND_MS = 100  # NFR-056: a lagging reader's samples are at most this old (median)


class FreshSampleService(psmove_hid_pb2_grpc.ControllerIOServiceServicer):
    """StreamIO sends SAMPLE_RATE_HZ samples; each one carries its build time (ms) in ``temperature``."""

    def __init__(self, epoch: float):
        self.epoch = epoch

    def now_ms(self) -> int:
        return int((time.monotonic() - self.epoch) * 1000)

    def StreamIO(self, request_iterator, context):  # noqa: N802 (gRPC method name)
        period = 1.0 / SAMPLE_RATE_HZ
        next_at = time.monotonic()
        index = 0
        while context.is_active():
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_at = time.monotonic()  # behind: send the newest sample now, no catch-up burst
            next_at += period
            yield psmove_hid_pb2.SensorData(
                serial=f"0006F7AABB{index % CONTROLLERS:02d}",
                battery=80,
                temperature=self.now_ms(),
                accel_x=0.1,
                accel_y=0.2,
                accel_z=0.9,
                gyro_x=12.0,
                gyro_y=-3.0,
                gyro_z=7.0,
            )
            index += 1


@pytest.fixture
def epoch():
    return time.monotonic()


@pytest.fixture
def hid_server(epoch):
    service = FreshSampleService(epoch)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    psmove_hid_pb2_grpc.add_ControllerIOServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    yield service, port
    server.stop(grace=0)


@pytest.fixture(params=[RustServiceAdapter, PythonHidAdapter], ids=["rust", "python_hid"])
def adapter(request, hid_server, monkeypatch):
    _, port = hid_server
    prefix = "RUST_HID" if request.param is RustServiceAdapter else "PYTHON_HID"
    monkeypatch.setenv(f"{prefix}_HOST", "127.0.0.1")
    monkeypatch.setenv(f"{prefix}_PORT", str(port))
    instance = request.param()
    yield instance
    instance._stop_stream()


@pytest.mark.uc("UC-002", flow="main", br=["NFR-056"])
def test_a_lagging_reader_gets_samples_at_most_100ms_old(adapter, hid_server, epoch, monkeypatch):
    """Reader at half the arrival rate: the median sample age stays under the bound."""
    service, _ = hid_server
    ages_ms: list[int] = []
    lock = threading.Lock()
    measure_from = epoch + WARM_UP_S
    convert = hid_stream.sensor_data_to_dict

    def slow_convert(data):
        if time.monotonic() >= measure_from:
            with lock:
                ages_ms.append(service.now_ms() - data.temperature)
        time.sleep(READER_DELAY_S)
        return convert(data)

    monkeypatch.setattr(hid_stream, "sensor_data_to_dict", slow_convert)
    adapter._ensure_stream()
    time.sleep(WARM_UP_S + MEASURE_S)
    adapter._stop_stream()

    with lock:
        ages = list(ages_ms)
    assert len(ages) > 100, f"the reader got only {len(ages)} samples"
    median = statistics.median(ages)
    print(f"sample age ms: median {median} max {max(ages)} n {len(ages)}")
    assert median < STALENESS_BOUND_MS, (
        f"median sample age {median} ms (max {max(ages)} ms) over {len(ages)} samples: "
        "the channel's receive window holds old samples"
    )


@pytest.mark.uc("UC-002", flow="main", br=["NFR-056"])
def test_a_reader_that_keeps_up_still_gets_every_controller_at_100hz(adapter, epoch, monkeypatch):
    """The small window must not starve the 100 Hz poll loop (NFR-002): each controller arrives at >= 100 Hz."""
    counts: dict[str, int] = {}
    lock = threading.Lock()
    measure_from = epoch + WARM_UP_S
    convert = hid_stream.sensor_data_to_dict

    def counting_convert(data):
        if time.monotonic() >= measure_from:
            with lock:
                counts[data.serial] = counts.get(data.serial, 0) + 1
        return convert(data)

    monkeypatch.setattr(hid_stream, "sensor_data_to_dict", counting_convert)
    adapter._ensure_stream()
    time.sleep(WARM_UP_S + MEASURE_S)
    adapter._stop_stream()

    with lock:
        rates = {serial: count / MEASURE_S for serial, count in counts.items()}
    assert len(rates) == CONTROLLERS, f"samples from {sorted(rates)} only"
    assert min(rates.values()) >= 100, f"per-controller rates {rates} (Hz)"
