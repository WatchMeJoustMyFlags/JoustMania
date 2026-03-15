"""
Async gRPC interceptors for OpenTelemetry trace context propagation.

The standard gRPC OpenTelemetry instrumentation doesn't reliably work with grpc.aio.
This module provides manual interceptors for both client and server:

CLIENT INTERCEPTORS:
- Inject W3C Trace Context (traceparent, tracestate) into outgoing gRPC metadata
- Optionally create client spans (disabled by default, enable with OTEL_GRPC_CLIENT_SPANS=true)
- When grpc_rpc_spans flag is enabled, create CLIENT spans with full RPC semantic convention
  attributes for Dynatrace service topology (Smartscape)

SERVER INTERCEPTORS:
- Extract W3C Trace Context from incoming gRPC metadata
- Attach extracted context so service spans are linked to the parent trace
- When grpc_rpc_spans flag is enabled, create SERVER spans with full RPC semantic convention
  attributes for Dynatrace service topology (Smartscape)

Usage (Client):
    from lib.grpc_tracing import get_context_propagation_interceptors

    interceptors = get_context_propagation_interceptors()
    channel = grpc.aio.insecure_channel("service:50051", interceptors=interceptors)

Usage (Server):
    from lib.grpc_tracing import get_server_interceptors

    interceptors = get_server_interceptors()
    server = grpc.aio.server(options=options, interceptors=interceptors)
"""

import inspect
import logging
import os
from collections.abc import Callable
from typing import Any

import grpc
import grpc.aio
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import SpanKind, Status, StatusCode

logger = logging.getLogger(__name__)

# Configuration: Set OTEL_GRPC_CLIENT_SPANS=true to enable client spans
_CREATE_CLIENT_SPANS = os.getenv("OTEL_GRPC_CLIENT_SPANS", "false").lower() == "true"

# Span attribute keys (S1192 - avoid duplicate string literals)
RPC_SYSTEM_ATTR = "rpc.system"
RPC_METHOD_ATTR = "rpc.method"
RPC_SERVICE_ATTR = "rpc.service"
RPC_GRPC_STATUS_CODE_ATTR = "rpc.grpc.status_code"
SERVER_ADDRESS_ATTR = "server.address"
SERVER_PORT_ATTR = "server.port"


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

    path = method.lstrip("/")
    parts = path.split("/")
    if len(parts) == 2:
        service_part = parts[0]
        service_name = service_part.rsplit(".", 1)[-1] if "." in service_part else service_part
        return (service_name, parts[1])

    return ("unknown", method)


def _rpc_spans_enabled() -> bool:
    """Check if the grpc_rpc_spans feature flag is enabled.

    Evaluated at runtime on each RPC call to support hot-reload via flagd.
    Returns False if flagd is unavailable or the flag is not defined.
    """
    try:
        from lib.feature_flags import get_flag_client

        client = get_flag_client("performance")
        return client.get_boolean_value("grpc_rpc_spans", False)
    except Exception:
        return False


def _safe_detach(token: object) -> None:
    """Detach an OpenTelemetry context token, ignoring ValueError.

    When a gRPC stream disconnects (e.g., GeneratorExit during cleanup),
    the token may have been created in a different async context. The detach
    raises ValueError in this case, but it's harmless because the context
    is being cleaned up anyway.
    """
    try:
        otel_context.detach(token)
    except ValueError:
        logger.debug("OTEL context detach failed (token from different context) - safe to ignore")


def _prepare_metadata(existing_metadata: Any) -> tuple:
    """Prepare metadata with trace context injection.

    Args:
        existing_metadata: Existing metadata (can be None, tuple, list, or grpc.aio.Metadata)

    Returns:
        Tuple of (key, value) pairs with trace context injected
    """
    # Convert existing metadata to dict, handling various input types
    # IMPORTANT: Do NOT use dict(metadata) - grpc.aio.Metadata has broken dict() behavior
    # that causes KeyError when iterating because it yields (key, value) tuples but
    # dict() treats them as keys to look up.
    metadata: dict[str, str] = {}
    if existing_metadata is not None:
        # Check for grpc.aio.Metadata or similar objects that iterate over (key, value) tuples
        # but shouldn't be passed to dict() constructor
        type_name = type(existing_metadata).__name__
        if type_name == "Metadata" or isinstance(existing_metadata, list | tuple):
            # Iterate and unpack tuples directly
            for item in existing_metadata:
                if isinstance(item, tuple | list) and len(item) >= 2:
                    metadata[str(item[0])] = str(item[1])
        elif isinstance(existing_metadata, dict):
            metadata = dict(existing_metadata)
        elif hasattr(existing_metadata, "items"):
            # Dict-like object with items() method
            for key, value in existing_metadata.items():
                metadata[str(key)] = str(value)

    inject(metadata)
    return tuple(metadata.items())


def _extract_method_name(method: str | bytes) -> str:
    """Extract clean method name from gRPC method path."""
    if isinstance(method, bytes):
        method = method.decode("utf-8")
    return method[1:] if method.startswith("/") else method


def _build_rpc_attributes(
    service: str,
    method: str,
    server_address: str | None = None,
    server_port: int | None = None,
) -> dict[str, Any]:
    """Build RPC semantic convention attributes for a span."""
    attrs: dict[str, Any] = {
        RPC_SYSTEM_ATTR: "grpc",
        RPC_SERVICE_ATTR: service,
        RPC_METHOD_ATTR: method,
    }
    if server_address is not None:
        attrs[SERVER_ADDRESS_ATTR] = server_address
    if server_port is not None:
        attrs[SERVER_PORT_ATTR] = server_port
    return attrs


class ContextPropagationInterceptor(grpc.aio.UnaryUnaryClientInterceptor):
    """
    Async unary-unary client interceptor for trace context propagation.

    Injects current trace context into gRPC metadata so server-side spans
    are linked to the parent trace. Optionally creates client spans when
    OTEL_GRPC_CLIENT_SPANS=true or when the grpc_rpc_spans flag is enabled.
    """

    def __init__(self, server_address: str | None = None, server_port: int | None = None):
        self._tracer = trace.get_tracer(__name__) if _CREATE_CLIENT_SPANS else None
        self._server_address = server_address
        self._server_port = server_port

    async def intercept_unary_unary(
        self,
        continuation: Callable,
        client_call_details: grpc.aio.ClientCallDetails,
        request: Any,
    ) -> Any:
        """Intercept unary-unary RPC calls to propagate trace context."""
        new_details = grpc.aio.ClientCallDetails(
            method=client_call_details.method,
            timeout=client_call_details.timeout,
            metadata=_prepare_metadata(client_call_details.metadata),
            credentials=client_call_details.credentials,
            wait_for_ready=client_call_details.wait_for_ready,
        )

        rpc_spans = _rpc_spans_enabled()

        if not _CREATE_CLIENT_SPANS and not rpc_spans:
            return await continuation(new_details, request)

        service, method = _parse_method(client_call_details.method)

        if rpc_spans:
            tracer = self._tracer or trace.get_tracer(__name__)
            attrs = _build_rpc_attributes(service, method, self._server_address, self._server_port)
            with tracer.start_as_current_span(f"{service}/{method}", kind=SpanKind.CLIENT, attributes=attrs) as span:
                try:
                    response = await continuation(new_details, request)
                    span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, grpc.StatusCode.OK.value[0])
                    span.set_status(Status(StatusCode.OK))
                    return response
                except grpc.aio.AioRpcError as e:
                    span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, e.code().value[0])
                    span.set_status(Status(StatusCode.ERROR, str(e.code())))
                    raise

        # Legacy OTEL_GRPC_CLIENT_SPANS path
        clean_method = _extract_method_name(client_call_details.method)
        with self._tracer.start_as_current_span(
            clean_method, kind=SpanKind.CLIENT, attributes={RPC_SYSTEM_ATTR: "grpc", RPC_METHOD_ATTR: clean_method}
        ) as span:
            try:
                response = await continuation(new_details, request)
                span.set_status(Status(StatusCode.OK))
                return response
            except grpc.aio.AioRpcError as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e.code())))
                raise


class ContextPropagationStreamUnaryInterceptor(grpc.aio.StreamUnaryClientInterceptor):
    """Async stream-unary client interceptor for trace context propagation."""

    def __init__(self, server_address: str | None = None, server_port: int | None = None):
        self._tracer = trace.get_tracer(__name__) if _CREATE_CLIENT_SPANS else None
        self._server_address = server_address
        self._server_port = server_port

    async def intercept_stream_unary(
        self,
        continuation: Callable,
        client_call_details: grpc.aio.ClientCallDetails,
        request_iterator: Any,
    ) -> Any:
        """Intercept stream-unary RPC calls to propagate trace context."""
        new_details = grpc.aio.ClientCallDetails(
            method=client_call_details.method,
            timeout=client_call_details.timeout,
            metadata=_prepare_metadata(client_call_details.metadata),
            credentials=client_call_details.credentials,
            wait_for_ready=client_call_details.wait_for_ready,
        )

        rpc_spans = _rpc_spans_enabled()

        if not _CREATE_CLIENT_SPANS and not rpc_spans:
            return await continuation(new_details, request_iterator)

        service, method = _parse_method(client_call_details.method)

        if rpc_spans:
            tracer = self._tracer or trace.get_tracer(__name__)
            attrs = _build_rpc_attributes(service, method, self._server_address, self._server_port)
            with tracer.start_as_current_span(f"{service}/{method}", kind=SpanKind.CLIENT, attributes=attrs) as span:
                try:
                    response = await continuation(new_details, request_iterator)
                    span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, grpc.StatusCode.OK.value[0])
                    span.set_status(Status(StatusCode.OK))
                    return response
                except grpc.aio.AioRpcError as e:
                    span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, e.code().value[0])
                    span.set_status(Status(StatusCode.ERROR, str(e.code())))
                    raise

        # Legacy OTEL_GRPC_CLIENT_SPANS path
        clean_method = _extract_method_name(client_call_details.method)
        with self._tracer.start_as_current_span(
            clean_method, kind=SpanKind.CLIENT, attributes={RPC_SYSTEM_ATTR: "grpc", RPC_METHOD_ATTR: clean_method}
        ) as span:
            try:
                response = await continuation(new_details, request_iterator)
                span.set_status(Status(StatusCode.OK))
                return response
            except grpc.aio.AioRpcError as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e.code())))
                raise


class ContextPropagationUnaryStreamInterceptor(grpc.aio.UnaryStreamClientInterceptor):
    """Async unary-stream client interceptor for trace context propagation."""

    def __init__(self, server_address: str | None = None, server_port: int | None = None):
        self._tracer = trace.get_tracer(__name__) if _CREATE_CLIENT_SPANS else None
        self._server_address = server_address
        self._server_port = server_port

    async def intercept_unary_stream(
        self,
        continuation: Callable,
        client_call_details: grpc.aio.ClientCallDetails,
        request: Any,
    ) -> Any:
        """Intercept unary-stream RPC calls to propagate trace context."""
        new_details = grpc.aio.ClientCallDetails(
            method=client_call_details.method,
            timeout=client_call_details.timeout,
            metadata=_prepare_metadata(client_call_details.metadata),
            credentials=client_call_details.credentials,
            wait_for_ready=client_call_details.wait_for_ready,
        )

        rpc_spans = _rpc_spans_enabled()

        if not _CREATE_CLIENT_SPANS and not rpc_spans:
            return await continuation(new_details, request)

        service, method = _parse_method(client_call_details.method)

        if rpc_spans:
            tracer = self._tracer or trace.get_tracer(__name__)
            attrs = _build_rpc_attributes(service, method, self._server_address, self._server_port)
            with tracer.start_as_current_span(f"{service}/{method}", kind=SpanKind.CLIENT, attributes=attrs) as span:
                try:
                    call = await continuation(new_details, request)
                    span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, grpc.StatusCode.OK.value[0])
                    span.set_status(Status(StatusCode.OK))
                    return call
                except grpc.aio.AioRpcError as e:
                    span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, e.code().value[0])
                    span.set_status(Status(StatusCode.ERROR, str(e.code())))
                    raise

        # Legacy OTEL_GRPC_CLIENT_SPANS path
        clean_method = _extract_method_name(client_call_details.method)
        with self._tracer.start_as_current_span(
            clean_method, kind=SpanKind.CLIENT, attributes={RPC_SYSTEM_ATTR: "grpc", RPC_METHOD_ATTR: clean_method}
        ) as span:
            try:
                call = await continuation(new_details, request)
                span.set_status(Status(StatusCode.OK))
                return call
            except grpc.aio.AioRpcError as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e.code())))
                raise


class ContextPropagationStreamStreamInterceptor(grpc.aio.StreamStreamClientInterceptor):
    """Async stream-stream client interceptor for trace context propagation."""

    def __init__(self, server_address: str | None = None, server_port: int | None = None):
        self._tracer = trace.get_tracer(__name__) if _CREATE_CLIENT_SPANS else None
        self._server_address = server_address
        self._server_port = server_port

    async def intercept_stream_stream(
        self,
        continuation: Callable,
        client_call_details: grpc.aio.ClientCallDetails,
        request_iterator: Any,
    ) -> Any:
        """Intercept stream-stream RPC calls to propagate trace context."""
        new_details = grpc.aio.ClientCallDetails(
            method=client_call_details.method,
            timeout=client_call_details.timeout,
            metadata=_prepare_metadata(client_call_details.metadata),
            credentials=client_call_details.credentials,
            wait_for_ready=client_call_details.wait_for_ready,
        )

        rpc_spans = _rpc_spans_enabled()

        if not _CREATE_CLIENT_SPANS and not rpc_spans:
            return await continuation(new_details, request_iterator)

        service, method = _parse_method(client_call_details.method)

        if rpc_spans:
            tracer = self._tracer or trace.get_tracer(__name__)
            attrs = _build_rpc_attributes(service, method, self._server_address, self._server_port)
            with tracer.start_as_current_span(f"{service}/{method}", kind=SpanKind.CLIENT, attributes=attrs) as span:
                try:
                    call = await continuation(new_details, request_iterator)
                    span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, grpc.StatusCode.OK.value[0])
                    span.set_status(Status(StatusCode.OK))
                    return call
                except grpc.aio.AioRpcError as e:
                    span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, e.code().value[0])
                    span.set_status(Status(StatusCode.ERROR, str(e.code())))
                    raise

        # Legacy OTEL_GRPC_CLIENT_SPANS path
        clean_method = _extract_method_name(client_call_details.method)
        with self._tracer.start_as_current_span(
            clean_method, kind=SpanKind.CLIENT, attributes={RPC_SYSTEM_ATTR: "grpc", RPC_METHOD_ATTR: clean_method}
        ) as span:
            try:
                call = await continuation(new_details, request_iterator)
                span.set_status(Status(StatusCode.OK))
                return call
            except grpc.aio.AioRpcError as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e.code())))
                raise


def get_context_propagation_interceptors(
    server_address: str | None = None,
    server_port: int | None = None,
) -> list:
    """
    Get all context propagation interceptors for async gRPC clients.

    These interceptors inject W3C Trace Context headers into gRPC metadata
    so that server-side spans are linked to the parent trace. When the
    grpc_rpc_spans feature flag is enabled, they also create CLIENT spans
    with full RPC semantic convention attributes.

    Args:
        server_address: Target server hostname/IP (used in RPC span attributes).
        server_port: Target server port (used in RPC span attributes).

    Returns:
        List of interceptors covering all RPC patterns (unary-unary, stream-unary,
        unary-stream, stream-stream).
    """
    return [
        ContextPropagationInterceptor(server_address, server_port),
        ContextPropagationStreamUnaryInterceptor(server_address, server_port),
        ContextPropagationUnaryStreamInterceptor(server_address, server_port),
        ContextPropagationStreamStreamInterceptor(server_address, server_port),
    ]


# =============================================================================
# Server-side interceptors
# =============================================================================


def _extract_context_from_metadata(context: grpc.aio.ServicerContext) -> otel_context.Context:
    """Extract OpenTelemetry context from gRPC metadata.

    Args:
        context: gRPC servicer context containing request metadata

    Returns:
        OpenTelemetry context with extracted trace information
    """
    metadata: dict[str, str] = {}
    invocation_metadata = context.invocation_metadata()
    if invocation_metadata:
        for key, value in invocation_metadata:
            metadata[key] = value
    return extract(metadata)


class ServerUnaryUnaryInterceptor(grpc.aio.ServerInterceptor):
    """Server interceptor that extracts trace context and optionally creates SERVER spans."""

    def __init__(self):
        self._tracer = trace.get_tracer(__name__)

    async def intercept_service(
        self,
        continuation: Callable,
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        """Wrap the handler to extract trace context."""
        handler = await continuation(handler_call_details)

        if handler is None:
            return None

        rpc_spans = _rpc_spans_enabled()
        method_path = handler_call_details.method
        service, method = _parse_method(method_path) if rpc_spans else ("", "")

        if handler.unary_unary:
            original_handler = handler.unary_unary

            if rpc_spans:

                async def wrapped_unary_unary(request, context):
                    parent_ctx = _extract_context_from_metadata(context)
                    token = otel_context.attach(parent_ctx)
                    try:
                        attrs = _build_rpc_attributes(service, method)
                        with self._tracer.start_as_current_span(
                            f"{service}/{method}", kind=SpanKind.SERVER, attributes=attrs
                        ) as span:
                            try:
                                result = original_handler(request, context)
                                if inspect.iscoroutine(result):
                                    result = await result
                                span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, grpc.StatusCode.OK.value[0])
                                span.set_status(Status(StatusCode.OK))
                                return result
                            except grpc.aio.AioRpcError as e:
                                span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, e.code().value[0])
                                span.set_status(Status(StatusCode.ERROR, str(e.code())))
                                raise
                            except Exception as exc:
                                span.record_exception(exc)
                                span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, grpc.StatusCode.UNKNOWN.value[0])
                                span.set_status(Status(StatusCode.ERROR))
                                raise
                    finally:
                        _safe_detach(token)

            else:

                async def wrapped_unary_unary(request, context):
                    parent_ctx = _extract_context_from_metadata(context)
                    token = otel_context.attach(parent_ctx)
                    try:
                        result = original_handler(request, context)
                        if inspect.iscoroutine(result):
                            return await result
                        return result
                    finally:
                        _safe_detach(token)

            return grpc.unary_unary_rpc_method_handler(
                wrapped_unary_unary,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        if handler.unary_stream:
            original_handler = handler.unary_stream

            if rpc_spans:

                async def wrapped_unary_stream(request, context):
                    parent_ctx = _extract_context_from_metadata(context)
                    token = otel_context.attach(parent_ctx)
                    try:
                        attrs = _build_rpc_attributes(service, method)
                        with self._tracer.start_as_current_span(
                            f"{service}/{method}", kind=SpanKind.SERVER, attributes=attrs
                        ) as span:
                            try:
                                async for response in original_handler(request, context):
                                    yield response
                                span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, grpc.StatusCode.OK.value[0])
                                span.set_status(Status(StatusCode.OK))
                            except grpc.aio.AioRpcError as e:
                                span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, e.code().value[0])
                                span.set_status(Status(StatusCode.ERROR, str(e.code())))
                                raise
                            except Exception as exc:
                                span.record_exception(exc)
                                span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, grpc.StatusCode.UNKNOWN.value[0])
                                span.set_status(Status(StatusCode.ERROR))
                                raise
                    finally:
                        _safe_detach(token)

            else:

                async def wrapped_unary_stream(request, context):
                    parent_ctx = _extract_context_from_metadata(context)
                    token = otel_context.attach(parent_ctx)
                    try:
                        async for response in original_handler(request, context):
                            yield response
                    finally:
                        _safe_detach(token)

            return grpc.unary_stream_rpc_method_handler(
                wrapped_unary_stream,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        if handler.stream_unary:
            original_handler = handler.stream_unary

            if rpc_spans:

                async def wrapped_stream_unary(request_iterator, context):
                    parent_ctx = _extract_context_from_metadata(context)
                    token = otel_context.attach(parent_ctx)
                    try:
                        attrs = _build_rpc_attributes(service, method)
                        with self._tracer.start_as_current_span(
                            f"{service}/{method}", kind=SpanKind.SERVER, attributes=attrs
                        ) as span:
                            try:
                                result = original_handler(request_iterator, context)
                                if inspect.iscoroutine(result):
                                    result = await result
                                span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, grpc.StatusCode.OK.value[0])
                                span.set_status(Status(StatusCode.OK))
                                return result
                            except grpc.aio.AioRpcError as e:
                                span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, e.code().value[0])
                                span.set_status(Status(StatusCode.ERROR, str(e.code())))
                                raise
                            except Exception as exc:
                                span.record_exception(exc)
                                span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, grpc.StatusCode.UNKNOWN.value[0])
                                span.set_status(Status(StatusCode.ERROR))
                                raise
                    finally:
                        _safe_detach(token)

            else:

                async def wrapped_stream_unary(request_iterator, context):
                    parent_ctx = _extract_context_from_metadata(context)
                    token = otel_context.attach(parent_ctx)
                    try:
                        result = original_handler(request_iterator, context)
                        if inspect.iscoroutine(result):
                            return await result
                        return result
                    finally:
                        _safe_detach(token)

            return grpc.stream_unary_rpc_method_handler(
                wrapped_stream_unary,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        if handler.stream_stream:
            original_handler = handler.stream_stream

            if rpc_spans:

                async def wrapped_stream_stream(request_iterator, context):
                    parent_ctx = _extract_context_from_metadata(context)
                    token = otel_context.attach(parent_ctx)
                    try:
                        attrs = _build_rpc_attributes(service, method)
                        with self._tracer.start_as_current_span(
                            f"{service}/{method}", kind=SpanKind.SERVER, attributes=attrs
                        ) as span:
                            try:
                                async for response in original_handler(request_iterator, context):
                                    yield response
                                span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, grpc.StatusCode.OK.value[0])
                                span.set_status(Status(StatusCode.OK))
                            except grpc.aio.AioRpcError as e:
                                span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, e.code().value[0])
                                span.set_status(Status(StatusCode.ERROR, str(e.code())))
                                raise
                            except Exception as exc:
                                span.record_exception(exc)
                                span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, grpc.StatusCode.UNKNOWN.value[0])
                                span.set_status(Status(StatusCode.ERROR))
                                raise
                    finally:
                        _safe_detach(token)

            else:

                async def wrapped_stream_stream(request_iterator, context):
                    parent_ctx = _extract_context_from_metadata(context)
                    token = otel_context.attach(parent_ctx)
                    try:
                        async for response in original_handler(request_iterator, context):
                            yield response
                    finally:
                        _safe_detach(token)

            return grpc.stream_stream_rpc_method_handler(
                wrapped_stream_stream,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        return handler


def get_server_interceptors() -> list:
    """
    Get server-side interceptors for trace context extraction.

    These interceptors extract W3C Trace Context from incoming gRPC metadata
    and attach it to the current OpenTelemetry context. When the grpc_rpc_spans
    feature flag is enabled, they also create SERVER spans with full RPC
    semantic convention attributes.

    Returns:
        List of server interceptors.
    """
    return [ServerUnaryUnaryInterceptor()]
