"""
Async gRPC interceptors for OpenTelemetry trace context propagation.

The standard gRPC OpenTelemetry instrumentation doesn't reliably work with grpc.aio.
This module provides manual interceptors for both client and server:

CLIENT INTERCEPTORS:
- Inject W3C Trace Context (traceparent, tracestate) into outgoing gRPC metadata: the CLIENT span
  when the call has one, so the server's span is its child, else the caller's current span (#1897)
- When grpc_rpc_spans flag is enabled, create CLIENT spans with full RPC semantic convention
  attributes for service topology in the trace backend. A CLIENT span covers the whole call:
  a unary call ends at its status, a stream when the RPC terminates (from the call's done
  callback, no per-message work); a non-OK status is ERROR, a cancel by the caller leaves the
  status unset (telemetry.md §3.1, #1879)

SERVER INTERCEPTORS:
- Extract W3C Trace Context from incoming gRPC metadata
- Attach extracted context so service spans are linked to the parent trace
- When grpc_rpc_spans flag is enabled, create SERVER spans with full RPC semantic convention
  attributes for service topology in the trace backend

Usage (Client):
    from lib.grpc_tracing import get_context_propagation_interceptors

    interceptors = get_context_propagation_interceptors()
    channel = grpc.aio.insecure_channel("service:50051", interceptors=interceptors)

Usage (Server):
    from lib.grpc_tracing import get_server_interceptors

    interceptors = get_server_interceptors()
    server = grpc.aio.server(options=options, interceptors=interceptors)
"""

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any

import grpc
import grpc.aio
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import SpanKind, Status, StatusCode

from lib.grpc_metrics import is_caller_cancel_details

logger = logging.getLogger(__name__)

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

        client = get_flag_client("observability")
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


async def _iterate_in_context(stream: Any, ctx: otel_context.Context):
    """Re-yield ``stream`` with ``ctx`` attached only while the inner iterator runs.

    Attaching once around a whole async generator breaks when the generator is closed
    from a different task (gRPC teardown, GeneratorExit): the detach token belongs to
    another contextvars Context and OpenTelemetry logs "Failed to detach context" (#1769).
    Attach and detach inside a single step, so both always happen in the same Context.

    Limitation: ``ctx`` is attached only while the inner iterator runs, so a handler must not
    hold a current span across a ``yield``. After the yield, spans the handler creates parent to
    ``ctx`` (the rpc span), not to the handler's own span. No current streaming handler does this.
    """
    iterator = stream.__aiter__()
    try:
        while True:
            token = otel_context.attach(ctx)
            try:
                item = await iterator.__anext__()
            except StopAsyncIteration:
                return
            finally:
                otel_context.detach(token)
            yield item
    finally:
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            token = otel_context.attach(ctx)
            try:
                await aclose()
            finally:
                otel_context.detach(token)


def _prepare_metadata(existing_metadata: Any, context: otel_context.Context | None = None) -> tuple:
    """Prepare metadata with trace context injection.

    Args:
        existing_metadata: Existing metadata (can be None, tuple, list, or grpc.aio.Metadata)
        context: Context whose span goes into ``traceparent``; None means the current context.
            An existing ``traceparent`` in ``existing_metadata`` is overwritten.

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

    inject(metadata, context=context)
    return tuple(metadata.items())


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


# The caller-cancel markers live in lib/grpc_metrics.py (CALLER_CANCEL_MARKERS), shared with the metrics.
_LOCAL_CANCEL_DETAILS = "Locally cancelled by application!"
_GC_CANCEL_DETAILS = "Cancelled upon garbage collection!"

# Pending end-of-stream tasks; held here so the loop does not drop them before they run.
_pending_span_finishes: set[asyncio.Task] = set()


def _is_caller_cancel(code: grpc.StatusCode, details: str | None) -> bool:
    return code == grpc.StatusCode.CANCELLED and is_caller_cancel_details(details)


def _record_client_status(
    span: trace.Span,
    code: grpc.StatusCode | None,
    details: str | None,
) -> None:
    """Record a client call's final gRPC status on its span (telemetry.md §3.1, #1879).

    OK sets status OK; a cancel by the caller sets only the code, the span status stays unset;
    any other code (a ``CANCELLED`` sent by the server included) sets ERROR.
    ``code=None`` (status unknown) records nothing.
    """
    if code is None:
        return
    span.set_attribute(RPC_GRPC_STATUS_CODE_ATTR, code.value[0])
    if code == grpc.StatusCode.OK:
        span.set_status(Status(StatusCode.OK))
    elif not _is_caller_cancel(code, details):
        span.set_status(Status(StatusCode.ERROR, str(code)))


class _ClientSpanFinisher:
    """Ends one CLIENT span exactly once, when its call terminates (#1879).

    Streams register :meth:`on_aio_call_done` / :meth:`on_sync_call_done` as the call's done
    callback, so nothing runs per message and a stream that is never read still ends its span
    (completed, failed, cancelled or garbage collected).
    """

    __slots__ = ("_done", "_loop", "_span")

    def __init__(self, span: trace.Span, loop: asyncio.AbstractEventLoop | None = None):
        self._span = span
        self._done = False
        # The aio call's loop, captured at setup: a done callback fired by GC on another thread
        # (a to_thread worker, a grpc or HID thread) hands the status read back to it.
        self._loop = loop

    def finish(self, code: grpc.StatusCode | None, details: str | None = None) -> None:
        if self._done:
            return
        self._done = True
        try:
            _record_client_status(self._span, code, details)
        finally:
            self._span.end()

    def fail(self, error: BaseException) -> None:
        """End the span for a setup failure that carries no gRPC status."""
        if self._done:
            return
        self._span.record_exception(error)
        self._span.set_status(Status(StatusCode.ERROR, type(error).__name__))
        self.finish(None)

    def on_aio_call_done(self, call: Any) -> None:
        # Runs inside grpc's cython layer: it must never raise. The final status is read through
        # coroutines that resolve at once on a finished call, so one task finishes the span.
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        try:
            if running is not None and (self._loop is None or running is self._loop):
                self._schedule_aio_finish(call)
            elif self._loop is not None and not self._loop.is_closed():
                # Off the loop thread (GC on another thread): schedule the read on the call's loop.
                self._loop.call_soon_threadsafe(self._schedule_aio_finish, call)
                # A stopped-but-not-closed loop never runs this read: the span is not exported (accepted).
            else:
                # No loop to read the status on: the call can only have been dropped (GC).
                self.finish(grpc.StatusCode.CANCELLED, _GC_CANCEL_DETAILS)
        except Exception:
            # e.g. the loop closed between the check and call_soon_threadsafe.
            logger.debug("Could not schedule the end of a gRPC client span; ending it as cancelled")
            self.finish(grpc.StatusCode.CANCELLED, _GC_CANCEL_DETAILS)

    def _schedule_aio_finish(self, call: Any) -> None:
        try:
            task = asyncio.get_running_loop().create_task(self._finish_from_aio_call(call))
        except Exception:
            logger.debug("Could not schedule the end of a gRPC client span; ending it without a status")
            self.finish(None)
            return
        _pending_span_finishes.add(task)
        task.add_done_callback(_pending_span_finishes.discard)

    async def _finish_from_aio_call(self, call: Any) -> None:
        code = details = None
        try:
            code = await call.code()
            details = await call.details()
        except Exception:
            logger.debug("Could not read a gRPC call status for its client span", exc_info=True)
        finally:
            self.finish(code, details)

    def on_sync_call_done(self, outcome: Any) -> None:
        try:
            code = grpc.StatusCode.CANCELLED if outcome.cancelled() else outcome.code()
            details = _LOCAL_CANCEL_DETAILS if outcome.cancelled() else outcome.details()
        except Exception:
            logger.debug("Could not read a sync gRPC call status for its client span", exc_info=True)
            code = details = None
        self.finish(code, details)


class _ClientSpanInterceptorBase:
    """Shared span setup for the async client interceptors.

    The ``traceparent`` carries the CLIENT span when the call has one, so the server's span is its
    child (telemetry.md §3.1, #1897); with no CLIENT span it carries the caller's current span.
    """

    def __init__(self, server_address: str | None = None, server_port: int | None = None):
        self._tracer = None  # tests inject a tracer; None = the global one
        self._server_address = server_address
        self._server_port = server_port

    def _span_spec(self, rpc_method: str | bytes) -> tuple[Any, str, dict[str, Any]] | None:
        """Return (tracer, name, attributes) for the call's span, or None for no span."""
        if _rpc_spans_enabled():
            service, method = _parse_method(rpc_method)
            tracer = self._tracer or trace.get_tracer(__name__)
            attrs = _build_rpc_attributes(service, method, self._server_address, self._server_port)
            return tracer, f"{service}/{method}", attrs
        return None

    async def _intercept_unary_response(self, continuation: Callable, details: Any, request: Any) -> Any:
        """Unary response: await the call, so the span ends at its final status."""
        spec = self._span_spec(details.method)
        if spec is None:
            return await continuation(_with_trace_metadata(details), request)
        tracer, name, attrs = spec
        with tracer.start_as_current_span(name, kind=SpanKind.CLIENT, attributes=attrs) as span:
            try:
                # Explicit context although the span is current: same code as the stream path.
                call = await continuation(_with_trace_metadata(details, trace.set_span_in_context(span)), request)
                await call
            except grpc.aio.AioRpcError as e:
                _record_client_status(span, e.code(), e.details())
                raise
            except asyncio.CancelledError:
                _record_client_status(span, grpc.StatusCode.CANCELLED, _LOCAL_CANCEL_DETAILS)
                raise
            _record_client_status(span, grpc.StatusCode.OK, None)
            return call

    async def _intercept_with_done_callback(self, continuation: Callable, details: Any, request: Any) -> Any:
        """Streams: return the call untouched and end the span from its done callback.

        No per-message work (``StreamGameplayData`` runs at 60 Hz), ``read()`` / ``write()`` /
        ``done_writing()`` reach grpc unchanged, and the span is never current in the caller, so
        no context token can be detached in another task (#1769).
        """
        spec = self._span_spec(details.method)
        if spec is None:
            return await continuation(_with_trace_metadata(details), request)
        tracer, name, attrs = spec
        span = tracer.start_span(name, kind=SpanKind.CLIENT, attributes=attrs)
        finisher = _ClientSpanFinisher(span, asyncio.get_running_loop())
        # The span goes into the metadata only, never attach(): it must not become current (#1769).
        span_ctx = trace.set_span_in_context(span)
        try:
            call = await continuation(_with_trace_metadata(details, span_ctx), request)
        except grpc.aio.AioRpcError as e:
            finisher.finish(e.code(), e.details())
            raise
        except asyncio.CancelledError:
            finisher.finish(grpc.StatusCode.CANCELLED, _LOCAL_CANCEL_DETAILS)
            raise
        except Exception as e:
            finisher.fail(e)
            raise
        except BaseException:
            finisher.finish(None)
            raise
        try:
            call.add_done_callback(finisher.on_aio_call_done)
        except Exception:
            logger.debug("gRPC call has no done callback; ending its client span at setup", exc_info=True)
            finisher.finish(None)
        return call


def _with_trace_metadata(
    client_call_details: grpc.aio.ClientCallDetails, context: otel_context.Context | None = None
) -> grpc.aio.ClientCallDetails:
    """Copy of ``client_call_details`` with ``context``'s span (default: the current one) injected."""
    return grpc.aio.ClientCallDetails(
        method=client_call_details.method,
        timeout=client_call_details.timeout,
        metadata=_prepare_metadata(client_call_details.metadata, context),
        credentials=client_call_details.credentials,
        wait_for_ready=client_call_details.wait_for_ready,
    )


class ContextPropagationInterceptor(_ClientSpanInterceptorBase, grpc.aio.UnaryUnaryClientInterceptor):
    """
    Async unary-unary client interceptor for trace context propagation.

    Injects trace context into gRPC metadata (the CLIENT span when there is one, else the
    caller's span) so server-side spans are linked to the parent trace. Creates client spans only
    when the grpc_rpc_spans flag is enabled.
    ``continuation`` returns the call before the RPC finishes, so the call is awaited here: the
    span ends at the final status (#1879).
    """

    async def intercept_unary_unary(
        self,
        continuation: Callable,
        client_call_details: grpc.aio.ClientCallDetails,
        request: Any,
    ) -> Any:
        """Intercept unary-unary RPC calls to propagate trace context."""
        return await self._intercept_unary_response(continuation, client_call_details, request)


class ContextPropagationStreamUnaryInterceptor(_ClientSpanInterceptorBase, grpc.aio.StreamUnaryClientInterceptor):
    """Async stream-unary client interceptor for trace context propagation.

    The call must be returned before its response arrives (the caller's ``write()`` waits for the
    interceptors), so the span ends from the call's done callback.
    """

    async def intercept_stream_unary(
        self,
        continuation: Callable,
        client_call_details: grpc.aio.ClientCallDetails,
        request_iterator: Any,
    ) -> Any:
        """Intercept stream-unary RPC calls to propagate trace context."""
        return await self._intercept_with_done_callback(continuation, client_call_details, request_iterator)


class ContextPropagationUnaryStreamInterceptor(_ClientSpanInterceptorBase, grpc.aio.UnaryStreamClientInterceptor):
    """Async unary-stream client interceptor for trace context propagation; the span ends with the stream."""

    async def intercept_unary_stream(
        self,
        continuation: Callable,
        client_call_details: grpc.aio.ClientCallDetails,
        request: Any,
    ) -> Any:
        """Intercept unary-stream RPC calls to propagate trace context."""
        return await self._intercept_with_done_callback(continuation, client_call_details, request)


class ContextPropagationStreamStreamInterceptor(_ClientSpanInterceptorBase, grpc.aio.StreamStreamClientInterceptor):
    """Async stream-stream client interceptor for trace context propagation; the span ends with the stream."""

    async def intercept_stream_stream(
        self,
        continuation: Callable,
        client_call_details: grpc.aio.ClientCallDetails,
        request_iterator: Any,
    ) -> Any:
        """Intercept stream-stream RPC calls to propagate trace context."""
        return await self._intercept_with_done_callback(continuation, client_call_details, request_iterator)


def get_context_propagation_interceptors(
    server_address: str | None = None,
    server_port: int | None = None,
) -> list:
    """
    Get all context propagation interceptors for async gRPC clients.

    These interceptors inject W3C Trace Context headers into gRPC metadata
    so that server-side spans are linked to the parent trace. When the
    grpc_rpc_spans feature flag is enabled, they also create CLIENT spans
    with full RPC semantic convention attributes, ended at the call's final status.

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
# Sync client interceptors (for blocking gRPC channels)
# =============================================================================


class SyncUnaryUnaryInterceptor(grpc.UnaryUnaryClientInterceptor):
    """Sync unary-unary client interceptor for trace context propagation.

    Used by adapters that use blocking gRPC channels (e.g. python_hid_adapter,
    rust_adapter) to inject W3C traceparent into outgoing RPCs. The sync ``continuation``
    never raises: it returns an outcome that carries the status, so the span ends from the
    outcome's done callback (inline for a blocking call, on a grpc thread for ``.future()``) (#1879).
    The ``traceparent`` carries the CLIENT span when the flag is on, else the caller's span (#1897).
    """

    def __init__(self, server_address: str | None = None, server_port: int | None = None):
        self._server_address = server_address
        self._server_port = server_port

    def intercept_unary_unary(self, continuation, client_call_details, request):
        if not _rpc_spans_enabled():
            return continuation(_sync_details_with_trace(client_call_details), request)

        service, method = _parse_method(client_call_details.method)
        tracer = trace.get_tracer(__name__)
        attrs = _build_rpc_attributes(service, method, self._server_address, self._server_port)
        span = tracer.start_span(f"{service}/{method}", kind=SpanKind.CLIENT, attributes=attrs)
        finisher = _ClientSpanFinisher(span)
        try:
            new_details = _sync_details_with_trace(client_call_details, trace.set_span_in_context(span))
            outcome = continuation(new_details, request)
        except Exception as e:
            finisher.fail(e)
            raise
        except BaseException:
            finisher.finish(None)
            raise
        outcome.add_done_callback(finisher.on_sync_call_done)
        return outcome


class SyncStreamStreamInterceptor(grpc.StreamStreamClientInterceptor):
    """Sync stream-stream client interceptor for trace context propagation.

    Used by adapters for bidirectional streaming RPCs (e.g. StreamIO).
    """

    def __init__(self, server_address: str | None = None, server_port: int | None = None):
        self._server_address = server_address
        self._server_port = server_port

    def intercept_stream_stream(self, continuation, client_call_details, request_iterator):
        # No CLIENT span on this stream: the caller's span is sent (telemetry.md §3.1).
        return continuation(_sync_details_with_trace(client_call_details), request_iterator)


class _ClientCallDetails(
    grpc.ClientCallDetails,
):
    """Wrapper for sync ClientCallDetails with updated metadata."""

    def __init__(self, method, timeout, metadata, credentials, wait_for_ready, compression):
        self.method = method
        self.timeout = timeout
        self.metadata = metadata
        self.credentials = credentials
        self.wait_for_ready = wait_for_ready
        self.compression = compression


def _sync_details_with_trace(details: Any, context: otel_context.Context | None = None) -> "_ClientCallDetails":
    """Copy of sync ``details`` with ``context``'s span (default: the current one) injected."""
    return _ClientCallDetails(
        details.method,
        details.timeout,
        _prepare_metadata(details.metadata, context),
        details.credentials,
        details.wait_for_ready,
        details.compression,
    )


def get_sync_context_propagation_interceptors(
    server_address: str | None = None,
    server_port: int | None = None,
) -> list:
    """Get context propagation interceptors for sync (blocking) gRPC channels.

    Use with grpc.intercept_channel() to add trace context to outgoing RPCs
    on sync channels (e.g. HID adapter → python-hid/rust-hid).

    Args:
        server_address: Target server hostname/IP (used in RPC span attributes).
        server_port: Target server port (used in RPC span attributes).

    Returns:
        List of sync interceptors for unary-unary and stream-stream RPCs.
    """
    return [
        SyncUnaryUnaryInterceptor(server_address, server_port),
        SyncStreamStreamInterceptor(server_address, server_port),
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
                    attrs = _build_rpc_attributes(service, method)
                    span = self._tracer.start_span(
                        f"{service}/{method}", context=parent_ctx, kind=SpanKind.SERVER, attributes=attrs
                    )
                    stream = _iterate_in_context(
                        original_handler(request, context), trace.set_span_in_context(span, parent_ctx)
                    )
                    try:
                        async for response in stream:
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
                        await stream.aclose()
                        span.end()

            else:

                async def wrapped_unary_stream(request, context):
                    parent_ctx = _extract_context_from_metadata(context)
                    stream = _iterate_in_context(original_handler(request, context), parent_ctx)
                    try:
                        async for response in stream:
                            yield response
                    finally:
                        await stream.aclose()

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
                    attrs = _build_rpc_attributes(service, method)
                    span = self._tracer.start_span(
                        f"{service}/{method}", context=parent_ctx, kind=SpanKind.SERVER, attributes=attrs
                    )
                    stream = _iterate_in_context(
                        original_handler(request_iterator, context), trace.set_span_in_context(span, parent_ctx)
                    )
                    try:
                        async for response in stream:
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
                        await stream.aclose()
                        span.end()

            else:

                async def wrapped_stream_stream(request_iterator, context):
                    parent_ctx = _extract_context_from_metadata(context)
                    stream = _iterate_in_context(original_handler(request_iterator, context), parent_ctx)
                    try:
                        async for response in stream:
                            yield response
                    finally:
                        await stream.aclose()

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
