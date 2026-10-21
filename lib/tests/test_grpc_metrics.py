"""Unit tests for the gRPC client metrics interceptors."""

import asyncio
import threading
import time
from concurrent import futures
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import grpc.aio
import pytest
import pytest_asyncio

from lib.grpc_metrics import (
    MetricsStreamStreamInterceptor,
    MetricsStreamUnaryInterceptor,
    MetricsUnaryStreamInterceptor,
    MetricsUnaryUnaryInterceptor,
    _parse_method,
    get_metrics_interceptors,
    get_sync_metrics_interceptors,
)
from lib.otel_metrics import disable_metrics_for_tests

# Disable OTEL metrics to prevent connection attempts during tests.
# This is safe to call after imports because the module-level metrics
# in grpc_metrics.py use lazy initialization and silently no-op when
# not initialized.
disable_metrics_for_tests()


class TestParseMethod:
    """Tests for the _parse_method helper."""

    def test_standard_format(self):
        """Test standard /pkg.ServiceName/MethodName format."""
        service, method = _parse_method("/pkg.ServiceName/MethodName")
        assert service == "ServiceName"
        assert method == "MethodName"

    def test_bytes_input(self):
        """Test that bytes input is decoded and parsed correctly."""
        service, method = _parse_method(b"/pkg.ServiceName/MethodName")
        assert service == "ServiceName"
        assert method == "MethodName"

    def test_no_leading_slash(self):
        """Test that input without leading slash still parses correctly."""
        service, method = _parse_method("pkg.ServiceName/MethodName")
        assert service == "ServiceName"
        assert method == "MethodName"

    def test_no_package_prefix(self):
        """Test service name without package prefix."""
        service, method = _parse_method("/ServiceName/MethodName")
        assert service == "ServiceName"
        assert method == "MethodName"

    def test_fallback_unexpected_format(self):
        """Test fallback for unexpected format returns ('unknown', original_method)."""
        service, method = _parse_method("just-a-string")
        assert service == "unknown"
        assert method == "just-a-string"

    def test_deeply_nested_package(self):
        """Test deeply nested package name extracts only service name."""
        service, method = _parse_method("/com.example.api.v1.SettingsService/GetSetting")
        assert service == "SettingsService"
        assert method == "GetSetting"

    def test_bytes_no_leading_slash(self):
        """Test bytes input without leading slash."""
        service, method = _parse_method(b"pkg.SvcName/DoThing")
        assert service == "SvcName"
        assert method == "DoThing"


class TestMetricsStreamUnaryInterceptor:
    """Tests for the stream-unary metrics interceptor."""

    @pytest.mark.asyncio
    async def test_records_duration_on_success(self):
        """Test that duration is recorded for stream-unary calls."""
        interceptor = MetricsStreamUnaryInterceptor()

        mock_response = MagicMock()
        continuation = AsyncMock(return_value=mock_response)

        call_details = MagicMock(spec=grpc.aio.ClientCallDetails)
        call_details.method = "/pkg.AudioService/PlaySound"

        with patch("lib.grpc_metrics.grpc_client_request_duration_seconds") as mock_histogram:
            mock_labeled = MagicMock()
            mock_histogram.labels.return_value = mock_labeled

            result = await interceptor.intercept_stream_unary(continuation, call_details, MagicMock())

            assert result == mock_response
            mock_histogram.labels.assert_called_once_with(service="AudioService", method="PlaySound")
            mock_labeled.observe.assert_called_once()


class TestGetMetricsInterceptors:
    """Tests for the get_metrics_interceptors factory."""

    def test_returns_four_interceptors(self):
        """Test that exactly 4 interceptors are returned."""
        interceptors = get_metrics_interceptors()
        assert len(interceptors) == 4

    def test_interceptor_types(self):
        """Test that interceptors cover all 4 RPC patterns."""
        interceptors = get_metrics_interceptors()
        types = {type(i) for i in interceptors}
        assert types == {
            MetricsUnaryUnaryInterceptor,
            MetricsUnaryStreamInterceptor,
            MetricsStreamUnaryInterceptor,
            MetricsStreamStreamInterceptor,
        }


# --- Real channels (#1426) ----------------------------------------------------------------------
# A loopback server with generic handlers (raw bytes, no proto) behind the real interceptor chain,
# so the tests see what grpc actually raises where: a mock continuation hides that the call is
# returned before the RPC finishes.

_SVC = "test.Probe"
_PATH = f"/{_SVC}/"


async def _aio_unary_ok(_request, _context):
    return b"ok"


async def _aio_unary_fail(_request, context):
    await context.abort(grpc.StatusCode.UNAVAILABLE, "down")


async def _aio_unary_slow(_request, _context):
    await asyncio.sleep(30)
    return b"late"


async def _aio_stream_ok(_request, _context):
    for _ in range(3):
        yield b"m"


async def _aio_stream_fail(_request, context):
    yield b"m"
    await context.abort(grpc.StatusCode.UNAVAILABLE, "dropped mid-stream")


async def _aio_stream_server_cancel(_request, context):
    yield b"m"
    await context.abort(grpc.StatusCode.CANCELLED, "server cancelled")


async def _aio_stream_forever(_request, _context):
    while True:
        yield b"m"
        await asyncio.sleep(0.01)


async def _aio_stream_many(request, _context):
    for _ in range(int(request or b"1000")):
        yield b"m"


async def _aio_bidi_fail(request_iterator, context):
    async for request in request_iterator:
        yield request
        await context.abort(grpc.StatusCode.INTERNAL, "bidi broke")


@pytest_asyncio.fixture
async def aio_channel():
    server = grpc.aio.server()
    handler = grpc.method_handlers_generic_handler(
        _SVC,
        {
            "UnaryOk": grpc.unary_unary_rpc_method_handler(_aio_unary_ok),
            "UnaryFail": grpc.unary_unary_rpc_method_handler(_aio_unary_fail),
            "UnarySlow": grpc.unary_unary_rpc_method_handler(_aio_unary_slow),
            "StreamOk": grpc.unary_stream_rpc_method_handler(_aio_stream_ok),
            "StreamFail": grpc.unary_stream_rpc_method_handler(_aio_stream_fail),
            "StreamServerCancel": grpc.unary_stream_rpc_method_handler(_aio_stream_server_cancel),
            "StreamForever": grpc.unary_stream_rpc_method_handler(_aio_stream_forever),
            "StreamMany": grpc.unary_stream_rpc_method_handler(_aio_stream_many),
            "BidiFail": grpc.stream_stream_rpc_method_handler(_aio_bidi_fail),
        },
    )
    server.add_generic_rpc_handlers((handler,))
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}", interceptors=get_metrics_interceptors())
    yield channel
    await channel.close()
    await server.stop(None)


@pytest.fixture
def metrics():
    """Patch both metrics; ``errors()`` lists the counted codes, ``observed()`` the durations."""
    with (
        patch("lib.grpc_metrics.grpc_client_errors_total") as counter,
        patch("lib.grpc_metrics.grpc_client_request_duration_seconds") as histogram,
    ):

        class _Metrics:
            def errors(self):
                return [
                    (c.kwargs["service"], c.kwargs["method"], c.kwargs["code"]) for c in counter.labels.call_args_list
                ]

            def observed(self):
                return [c.kwargs for c in histogram.labels.call_args_list]

            counter_calls = counter
            histogram_calls = histogram

        yield _Metrics()


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for the done callback"
        time.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.uc("UC-035", flow="main", steps=[6])
class TestAioClientErrors:
    """grpc_client_errors_total counts what the panels claim (telemetry.md §6, #1426)."""

    async def test_unary_error_status_is_counted(self, aio_channel, metrics):
        with pytest.raises(grpc.aio.AioRpcError):
            await aio_channel.unary_unary(_PATH + "UnaryFail")(b"x")

        assert metrics.errors() == [("Probe", "UnaryFail", "UNAVAILABLE")]
        assert metrics.observed() == [{"service": "Probe", "method": "UnaryFail"}]

    async def test_unary_success_is_timed_not_counted(self, aio_channel, metrics):
        assert await aio_channel.unary_unary(_PATH + "UnaryOk")(b"x") == b"ok"

        assert metrics.errors() == []
        assert metrics.observed() == [{"service": "Probe", "method": "UnaryOk"}]

    async def test_unary_local_cancel_is_not_counted(self, aio_channel, metrics):
        task = asyncio.create_task(aio_channel.unary_unary(_PATH + "UnarySlow")(b"x"))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert metrics.errors() == []

    async def test_mid_stream_error_is_counted_and_stream_timed_at_end(self, aio_channel, metrics):
        received = []
        with pytest.raises(grpc.aio.AioRpcError):
            async for message in aio_channel.unary_stream(_PATH + "StreamFail")(b"x"):
                received.append(message)

        assert received == [b"m"]
        assert metrics.errors() == [("Probe", "StreamFail", "UNAVAILABLE")]
        assert metrics.observed() == [{"service": "Probe", "method": "StreamFail"}]

    async def test_mid_stream_error_is_counted_through_read(self, aio_channel, metrics):
        call = aio_channel.unary_stream(_PATH + "StreamFail")(b"x")
        assert await call.read() == b"m"
        with pytest.raises(grpc.aio.AioRpcError):
            await call.read()

        assert metrics.errors() == [("Probe", "StreamFail", "UNAVAILABLE")]

    async def test_bidi_mid_stream_error_is_counted(self, aio_channel, metrics):
        call = aio_channel.stream_stream(_PATH + "BidiFail")()
        await call.write(b"x")
        with pytest.raises(grpc.aio.AioRpcError):
            async for _ in call:
                pass

        assert metrics.errors() == [("Probe", "BidiFail", "INTERNAL")]

    async def test_server_sent_cancelled_counts_like_other_codes(self, aio_channel, metrics):
        with pytest.raises(grpc.aio.AioRpcError):
            async for _ in aio_channel.unary_stream(_PATH + "StreamServerCancel")(b"x"):
                pass

        assert metrics.errors() == [("Probe", "StreamServerCancel", "CANCELLED")]

    async def test_stream_completed_is_timed_not_counted(self, aio_channel, metrics):
        received = [m async for m in aio_channel.unary_stream(_PATH + "StreamOk")(b"x")]

        assert received == [b"m"] * 3
        assert metrics.errors() == []
        assert metrics.observed() == [{"service": "Probe", "method": "StreamOk"}]

    async def test_stream_cancelled_by_call_cancel_is_not_counted(self, aio_channel, metrics):
        call = aio_channel.unary_stream(_PATH + "StreamForever")(b"x")
        with pytest.raises(asyncio.CancelledError):
            async for _ in call:
                call.cancel()

        assert metrics.errors() == []
        assert metrics.observed() == [{"service": "Probe", "method": "StreamForever"}]

    async def test_stream_reader_task_cancelled_is_not_counted(self, aio_channel, metrics):
        first = asyncio.Event()

        async def reader():
            async for _ in aio_channel.unary_stream(_PATH + "StreamForever")(b"x"):
                first.set()

        task = asyncio.create_task(reader())
        await first.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert metrics.errors() == []
        assert metrics.observed() == [{"service": "Probe", "method": "StreamForever"}]

    async def test_no_metric_call_per_stream_message(self, aio_channel, metrics):
        """Hot path (StreamGameplayData at 60 Hz): the metrics are touched once per stream, not per message."""
        count = 0
        async for _ in aio_channel.unary_stream(_PATH + "StreamMany")(b"500"):
            count += 1

        assert count == 500
        assert metrics.counter_calls.labels.call_count == 0
        assert metrics.histogram_calls.labels.call_count == 1


def _sync_unary_fail(_request, context):
    context.abort(grpc.StatusCode.UNAVAILABLE, "down")


def _sync_bidi_fail(request_iterator, context):
    for request in request_iterator:
        yield request
        context.abort(grpc.StatusCode.UNAVAILABLE, "hid service dropped")


def _sync_bidi_echo(request_iterator, _context):
    yield from request_iterator


@pytest.fixture
def sync_channel():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    handler = grpc.method_handlers_generic_handler(
        _SVC,
        {
            "UnaryOk": grpc.unary_unary_rpc_method_handler(lambda _request, _context: b"ok"),
            "UnaryFail": grpc.unary_unary_rpc_method_handler(_sync_unary_fail),
            "BidiFail": grpc.stream_stream_rpc_method_handler(_sync_bidi_fail),
            "BidiEcho": grpc.stream_stream_rpc_method_handler(_sync_bidi_echo),
        },
    )
    server.add_generic_rpc_handlers((handler,))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.intercept_channel(grpc.insecure_channel(f"127.0.0.1:{port}"), *get_sync_metrics_interceptors())
    yield channel
    channel.close()
    server.stop(None)


@pytest.mark.uc("UC-035", flow="main", steps=[6])
class TestSyncClientErrors:
    """Sync channels (controller-manager HID adapters) count like async ones (#1426)."""

    def test_unary_error_status_is_counted(self, sync_channel, metrics):
        with pytest.raises(grpc.RpcError):
            sync_channel.unary_unary(_PATH + "UnaryFail")(b"x")

        assert metrics.errors() == [("Probe", "UnaryFail", "UNAVAILABLE")]
        assert metrics.observed() == [{"service": "Probe", "method": "UnaryFail"}]

    def test_unary_success_is_timed_not_counted(self, sync_channel, metrics):
        assert sync_channel.unary_unary(_PATH + "UnaryOk")(b"x") == b"ok"

        assert metrics.errors() == []
        assert metrics.observed() == [{"service": "Probe", "method": "UnaryOk"}]

    def test_mid_stream_error_is_counted(self, sync_channel, metrics):
        responses = sync_channel.stream_stream(_PATH + "BidiFail")(iter([b"a", b"b"]))
        with pytest.raises(grpc.RpcError):
            for _ in responses:
                pass

        _wait_until(lambda: metrics.observed())
        assert metrics.errors() == [("Probe", "BidiFail", "UNAVAILABLE")]

    def test_local_cancel_is_not_counted(self, sync_channel, metrics):
        stop = threading.Event()

        def requests():
            yield b"a"
            stop.wait(5)

        responses = sync_channel.stream_stream(_PATH + "BidiEcho")(requests())
        assert next(responses) == b"a"
        responses.cancel()
        stop.set()

        _wait_until(lambda: metrics.observed())
        assert metrics.errors() == []

    def test_stream_completed_is_timed_not_counted(self, sync_channel, metrics):
        assert list(sync_channel.stream_stream(_PATH + "BidiEcho")(iter([b"a", b"b"]))) == [b"a", b"b"]

        _wait_until(lambda: metrics.observed())
        assert metrics.errors() == []
        assert metrics.observed() == [{"service": "Probe", "method": "BidiEcho"}]
