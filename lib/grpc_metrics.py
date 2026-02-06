"""
Async gRPC client interceptors for recording latency and error metrics.

Records per-RPC histogram latency and error counters for all outbound gRPC calls.
Metrics are exported via OpenTelemetry to the OTEL Collector for Prometheus scraping.

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


class MetricsUnaryUnaryInterceptor(grpc.aio.UnaryUnaryClientInterceptor):
    """Async unary-unary client interceptor that records latency and error metrics."""

    async def intercept_unary_unary(
        self,
        continuation: Callable,
        client_call_details: grpc.aio.ClientCallDetails,
        request: Any,
    ) -> Any:
        service, method = _parse_method(client_call_details.method)
        start = time.monotonic()
        try:
            response = await continuation(client_call_details, request)
            duration = time.monotonic() - start
            grpc_client_request_duration_seconds.labels(service=service, method=method).observe(duration)
            return response
        except grpc.aio.AioRpcError as e:
            duration = time.monotonic() - start
            grpc_client_request_duration_seconds.labels(service=service, method=method).observe(duration)
            grpc_client_errors_total.labels(service=service, method=method, code=e.code().name).inc()
            raise


class MetricsUnaryStreamInterceptor(grpc.aio.UnaryStreamClientInterceptor):
    """Async unary-stream client interceptor that records latency and error metrics."""

    async def intercept_unary_stream(
        self,
        continuation: Callable,
        client_call_details: grpc.aio.ClientCallDetails,
        request: Any,
    ) -> Any:
        service, method = _parse_method(client_call_details.method)
        start = time.monotonic()
        try:
            response = await continuation(client_call_details, request)
            duration = time.monotonic() - start
            grpc_client_request_duration_seconds.labels(service=service, method=method).observe(duration)
            return response
        except grpc.aio.AioRpcError as e:
            duration = time.monotonic() - start
            grpc_client_request_duration_seconds.labels(service=service, method=method).observe(duration)
            grpc_client_errors_total.labels(service=service, method=method, code=e.code().name).inc()
            raise


class MetricsStreamUnaryInterceptor(grpc.aio.StreamUnaryClientInterceptor):
    """Async stream-unary client interceptor that records latency and error metrics."""

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
            duration = time.monotonic() - start
            grpc_client_request_duration_seconds.labels(service=service, method=method).observe(duration)
            return response
        except grpc.aio.AioRpcError as e:
            duration = time.monotonic() - start
            grpc_client_request_duration_seconds.labels(service=service, method=method).observe(duration)
            grpc_client_errors_total.labels(service=service, method=method, code=e.code().name).inc()
            raise


class MetricsStreamStreamInterceptor(grpc.aio.StreamStreamClientInterceptor):
    """Async stream-stream client interceptor that records latency and error metrics."""

    async def intercept_stream_stream(
        self,
        continuation: Callable,
        client_call_details: grpc.aio.ClientCallDetails,
        request_iterator: Any,
    ) -> Any:
        service, method = _parse_method(client_call_details.method)
        start = time.monotonic()
        try:
            response = await continuation(client_call_details, request_iterator)
            duration = time.monotonic() - start
            grpc_client_request_duration_seconds.labels(service=service, method=method).observe(duration)
            return response
        except grpc.aio.AioRpcError as e:
            duration = time.monotonic() - start
            grpc_client_request_duration_seconds.labels(service=service, method=method).observe(duration)
            grpc_client_errors_total.labels(service=service, method=method, code=e.code().name).inc()
            raise


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
