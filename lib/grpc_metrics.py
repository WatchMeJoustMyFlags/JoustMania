"""
gRPC client interceptors for recording latency and error metrics.

Records per-RPC histogram latency and error counters for outbound gRPC calls (telemetry.md §6):
an error status at call setup, at the end of a unary call or while a response stream is read
counts in ``grpc_client_errors_total``; a cancel by the caller does not. Async channels use
``get_metrics_interceptors()`` (``lib.grpc_utils.create_channel`` adds them), sync channels
``get_sync_metrics_interceptors()``. Metrics are exported via OpenTelemetry to the OTEL Collector.

Usage:
    from lib.grpc_metrics import get_metrics_interceptors

    interceptors = get_metrics_interceptors()
    channel = grpc.aio.insecure_channel("service:50051", interceptors=interceptors)
"""

import time
from collections.abc import Callable
from typing import Any

import grpc
import grpc.aio

from lib.otel_metrics import Counter, Histogram

# Module-level metrics using the project's OTEL push metrics helpers.
# These are lazily initialized when init_metrics() is called in each service.
grpc_client_request_duration_seconds = Histogram(
    "grpc_client_request_duration_seconds",
    "Duration of outbound gRPC client calls in seconds",
    labelnames=["service", "method"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

grpc_client_errors_total = Counter(
    "grpc_client_errors_total",
    "Total outbound gRPC client errors",
    labelnames=["service", "method", "code"],
)


def _parse_method(method: str | bytes) -> tuple[str, str]:
    """Parse a gRPC method string into (service_name, method_name).

    gRPC methods follow the format ``/pkg.ServiceName/MethodName``.

    Args:
        method: The full gRPC method path, e.g. ``/pkg.ServiceName/MethodName``.
                May be bytes (decoded to str automatically).

    Returns:
        Tuple of (service_name, method_name). Falls back to ("unknown", method)
        when the format is unexpected.
    """
    if isinstance(method, bytes):
        method = method.decode("utf-8")

    # Strip leading slash if present
    path = method.lstrip("/")

    # Expect "pkg.ServiceName/MethodName"
    parts = path.split("/")
    if len(parts) == 2:
        # Extract just the service name (after the last dot in the package-qualified name)
        service_part = parts[0]
        service_name = service_part.rsplit(".", 1)[-1] if "." in service_part else service_part
        return (service_name, parts[1])

    return ("unknown", method)


def _observe_duration(service: str, method: str, start: float) -> None:
    grpc_client_request_duration_seconds.labels(service=service, method=method).observe(time.monotonic() - start)


def _count_error(service: str, method: str, code: grpc.StatusCode) -> None:
    grpc_client_errors_total.labels(service=service, method=method, code=code.name).inc()


async def _metered_responses(call: Any, service: str, method: str, start: float):
    """Re-yield a response stream, counting an error raised while it is read (#1426).

    Hot path: ``StreamGameplayData`` runs at 60 Hz through this generator, so a message costs one
    generator resume and nothing else; the metrics are touched once, when the stream ends.
    A local cancel (``call.cancel()``, a cancelled task, a closed channel) surfaces as
    ``asyncio.CancelledError``, not ``AioRpcError``, so it is not counted. A ``CANCELLED`` status
    sent by the server is an ``AioRpcError`` and counts, as on every other path.
    """
    try:
        async for response in call:
            yield response
    except grpc.aio.AioRpcError as e:
        _count_error(service, method, e.code())
        raise
    finally:
        _observe_duration(service, method, start)


class MetricsUnaryUnaryInterceptor(grpc.aio.UnaryUnaryClientInterceptor):
    """Async unary-unary client interceptor that records latency and error metrics.

    ``continuation`` returns the call before the RPC finishes, so the call is awaited here: the
    duration is the full RPC and an error status is counted (#1426). A local cancel raises
    ``asyncio.CancelledError`` and is not counted.
    """

    async def intercept_unary_unary(
        self,
        continuation: Callable,
        client_call_details: grpc.aio.ClientCallDetails,
        request: Any,
    ) -> Any:
        service, method = _parse_method(client_call_details.method)
        start = time.monotonic()
        try:
            call = await continuation(client_call_details, request)
            await call
            return call
        except grpc.aio.AioRpcError as e:
            _count_error(service, method, e.code())
            raise
        finally:
            _observe_duration(service, method, start)


class MetricsUnaryStreamInterceptor(grpc.aio.UnaryStreamClientInterceptor):
    """Async unary-stream client interceptor: counts setup and mid-stream errors, times the stream.

    The duration is observed once, when the stream ends (completed, failed, cancelled or
    abandoned); a stream the caller never reads is not timed.
    """

    async def intercept_unary_stream(
        self,
        continuation: Callable,
        client_call_details: grpc.aio.ClientCallDetails,
        request: Any,
    ) -> Any:
        service, method = _parse_method(client_call_details.method)
        start = time.monotonic()
        try:
            call = await continuation(client_call_details, request)
        except grpc.aio.AioRpcError as e:
            _count_error(service, method, e.code())
            _observe_duration(service, method, start)
            raise
        return _metered_responses(call, service, method, start)


class MetricsStreamUnaryInterceptor(grpc.aio.StreamUnaryClientInterceptor):
    """Async stream-unary client interceptor that records latency and error metrics.

    Only call setup is measured: the call must be returned before the response arrives (the
    caller's ``write()`` waits for the interceptors), and no JoustMania proto defines a
    stream-unary RPC.
    """

    async def intercept_stream_unary(
        self,
        continuation: Callable,
        client_call_details: grpc.aio.ClientCallDetails,
        request_iterator: Any,
    ) -> Any:
        service, method = _parse_method(client_call_details.method)
        start = time.monotonic()
        try:
            response = await continuation(client_call_details, request_iterator)
            _observe_duration(service, method, start)
            return response
        except grpc.aio.AioRpcError as e:
            _observe_duration(service, method, start)
            _count_error(service, method, e.code())
            raise


class MetricsStreamStreamInterceptor(grpc.aio.StreamStreamClientInterceptor):
    """Async stream-stream client interceptor: counts setup and mid-stream errors, times the stream.

    Same contract as :class:`MetricsUnaryStreamInterceptor`. The returned call still supports
    ``write()`` / ``done_writing()``: grpc wraps the generator and keeps the request side.
    """

    async def intercept_stream_stream(
        self,
        continuation: Callable,
        client_call_details: grpc.aio.ClientCallDetails,
        request_iterator: Any,
    ) -> Any:
        service, method = _parse_method(client_call_details.method)
        start = time.monotonic()
        try:
            call = await continuation(client_call_details, request_iterator)
        except grpc.aio.AioRpcError as e:
            _count_error(service, method, e.code())
            _observe_duration(service, method, start)
            raise
        return _metered_responses(call, service, method, start)


def get_metrics_interceptors() -> list:
    """Return all 4 metrics interceptors for async gRPC clients.

    Returns:
        List of interceptors covering all RPC patterns (unary-unary,
        unary-stream, stream-unary, stream-stream).
    """
    return [
        MetricsUnaryUnaryInterceptor(),
        MetricsUnaryStreamInterceptor(),
        MetricsStreamUnaryInterceptor(),
        MetricsStreamStreamInterceptor(),
    ]


# grpc's own details for a call ended by closing the channel on this side (``grpc/_channel.py``).
# The text varies by path (e.g. "Channel closed!", "Channel closed due to fork", "Channel deallocated!"),
# so match substrings.
_LOCAL_CHANNEL_CLOSE_MARKERS = ("Channel closed", "Channel deallocated")


def _is_local_channel_close(details: str | None) -> bool:
    return any(marker in (details or "") for marker in _LOCAL_CHANNEL_CLOSE_MARKERS)


def _meter_sync_call(call: Any, service: str, method: str, start: float) -> Any:
    """Record a sync call's metrics once, when it finishes, through its done callback.

    No per-message work: a stream's messages pass through untouched. A local cancel
    (``call.cancel()``, garbage collection, closing the channel) is not counted.
    """

    def _on_done(finished: Any) -> None:
        code = finished.code()
        local_cancel = finished.cancelled() or (
            code == grpc.StatusCode.CANCELLED and _is_local_channel_close(finished.details())
        )
        if code is not None and code != grpc.StatusCode.OK and not local_cancel:
            _count_error(service, method, code)
        _observe_duration(service, method, start)

    call.add_done_callback(_on_done)
    return call


class SyncMetricsUnaryUnaryInterceptor(grpc.UnaryUnaryClientInterceptor):
    """Sync unary-unary client interceptor for blocking channels (controller-manager HID adapters)."""

    def intercept_unary_unary(self, continuation, client_call_details, request):
        service, method = _parse_method(client_call_details.method)
        start = time.monotonic()
        return _meter_sync_call(continuation(client_call_details, request), service, method, start)


class SyncMetricsStreamStreamInterceptor(grpc.StreamStreamClientInterceptor):
    """Sync stream-stream client interceptor for blocking channels (the HID adapters' ``StreamIO``)."""

    def intercept_stream_stream(self, continuation, client_call_details, request_iterator):
        service, method = _parse_method(client_call_details.method)
        start = time.monotonic()
        return _meter_sync_call(continuation(client_call_details, request_iterator), service, method, start)


def get_sync_metrics_interceptors() -> list:
    """Return the metrics interceptors for sync channels: put them first in ``grpc.intercept_channel``.

    Covers the RPC shapes the sync callers use (unary-unary and stream-stream).
    """
    return [SyncMetricsUnaryUnaryInterceptor(), SyncMetricsStreamStreamInterceptor()]
