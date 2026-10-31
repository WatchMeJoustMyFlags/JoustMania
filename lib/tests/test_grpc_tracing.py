"""Tests for lib/grpc_tracing.py"""

import asyncio
import gc
import importlib
import logging
import sys
import threading
import time
from concurrent import futures
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import grpc.aio
import pytest
import pytest_asyncio
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

# Add lib to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from lib import grpc_tracing
from lib.grpc_metrics import get_metrics_interceptors
from lib.grpc_tracing import (
    _build_rpc_attributes,
    _parse_method,
    _prepare_metadata,
    _rpc_spans_enabled,
    _safe_detach,
    get_context_propagation_interceptors,
    get_server_interceptors,
    get_sync_context_propagation_interceptors,
)


class TestParseMethod:
    """Tests for _parse_method helper."""

    def test_standard_format(self):
        service, method = _parse_method("/joustmania.GameCoordinator/StartGame")
        assert service == "GameCoordinator"
        assert method == "StartGame"

    def test_no_package(self):
        service, method = _parse_method("/GameCoordinator/StartGame")
        assert service == "GameCoordinator"
        assert method == "StartGame"

    def test_bytes_input(self):
        service, method = _parse_method(b"/pkg.AudioService/PlaySound")
        assert service == "AudioService"
        assert method == "PlaySound"

    def test_unexpected_format(self):
        service, method = _parse_method("BadFormat")
        assert service == "unknown"
        assert method == "BadFormat"

    def test_multiple_leading_slashes(self):
        service, method = _parse_method("///foo/bar")
        assert service == "foo"
        assert method == "bar"

    def test_nested_package(self):
        service, method = _parse_method("/com.example.pkg.MyService/DoStuff")
        assert service == "MyService"
        assert method == "DoStuff"


class TestBuildRpcAttributes:
    """Tests for _build_rpc_attributes helper."""

    def test_basic_attributes(self):
        attrs = _build_rpc_attributes("GameCoordinator", "StartGame")
        assert attrs["rpc.system"] == "grpc"
        assert attrs["rpc.service"] == "GameCoordinator"
        assert attrs["rpc.method"] == "StartGame"
        assert "server.address" not in attrs
        assert "server.port" not in attrs

    def test_with_server_info(self):
        attrs = _build_rpc_attributes("GameCoordinator", "StartGame", "localhost", 50053)
        assert attrs["server.address"] == "localhost"
        assert attrs["server.port"] == 50053

    def test_none_server_info_omitted(self):
        attrs = _build_rpc_attributes("Svc", "Method", None, None)
        assert "server.address" not in attrs
        assert "server.port" not in attrs


class TestRpcSpansEnabled:
    """Tests for _rpc_spans_enabled helper."""

    @patch("lib.grpc_tracing.get_flag_client", create=True)
    def test_enabled_when_flag_true(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_boolean_value.return_value = True
        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            result = _rpc_spans_enabled()
        assert result is True

    def test_disabled_when_flagd_unavailable(self):
        # When feature_flags import fails or raises, should return False
        with patch.dict("sys.modules", {"lib.feature_flags": None}):
            result = _rpc_spans_enabled()
        assert result is False

    @patch("lib.feature_flags.get_flag_client")
    def test_disabled_when_flag_false(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_boolean_value.return_value = False
        mock_get_client.return_value = mock_client
        result = _rpc_spans_enabled()
        assert result is False

    @patch("lib.feature_flags.get_flag_client")
    def test_disabled_when_exception(self, mock_get_client):
        mock_get_client.side_effect = RuntimeError("flagd down")
        result = _rpc_spans_enabled()
        assert result is False


class TestPrepareMetadata:
    """Tests for _prepare_metadata helper."""

    def test_none_metadata(self):
        result = _prepare_metadata(None)
        # Should return a tuple (possibly with trace headers injected)
        assert isinstance(result, tuple)

    def test_tuple_metadata(self):
        existing = (("key1", "value1"), ("key2", "value2"))
        result = _prepare_metadata(existing)

        assert isinstance(result, tuple)
        result_dict = dict(result)
        assert result_dict["key1"] == "value1"
        assert result_dict["key2"] == "value2"

    def test_list_metadata(self):
        existing = [("key1", "value1")]
        result = _prepare_metadata(existing)

        assert isinstance(result, tuple)
        result_dict = dict(result)
        assert result_dict["key1"] == "value1"

    def test_dict_metadata(self):
        existing = {"key1": "value1", "key2": "value2"}
        result = _prepare_metadata(existing)

        assert isinstance(result, tuple)
        result_dict = dict(result)
        assert result_dict["key1"] == "value1"
        assert result_dict["key2"] == "value2"


class TestSafeDetach:
    """Tests for _safe_detach helper."""

    @patch("lib.grpc_tracing.otel_context")
    def test_normal_detach(self, mock_otel_context):
        token = object()

        _safe_detach(token)

        mock_otel_context.detach.assert_called_once_with(token)

    @patch("lib.grpc_tracing.otel_context")
    def test_value_error_suppressed(self, mock_otel_context):
        mock_otel_context.detach.side_effect = ValueError("token from different context")
        token = object()

        # Should not raise
        _safe_detach(token)

        mock_otel_context.detach.assert_called_once_with(token)


class TestGetInterceptors:
    """Tests for get_context_propagation_interceptors and get_server_interceptors."""

    def test_client_interceptors_count(self):
        interceptors = get_context_propagation_interceptors()
        assert len(interceptors) == 4

    def test_client_interceptor_types(self):
        import grpc.aio

        interceptors = get_context_propagation_interceptors()

        assert isinstance(interceptors[0], grpc.aio.UnaryUnaryClientInterceptor)
        assert isinstance(interceptors[1], grpc.aio.StreamUnaryClientInterceptor)
        assert isinstance(interceptors[2], grpc.aio.UnaryStreamClientInterceptor)
        assert isinstance(interceptors[3], grpc.aio.StreamStreamClientInterceptor)

    def test_client_interceptors_with_address(self):
        interceptors = get_context_propagation_interceptors("game-coordinator", 50053)
        assert len(interceptors) == 4
        # Verify address is stored
        assert interceptors[0]._server_address == "game-coordinator"
        assert interceptors[0]._server_port == 50053

    def test_server_interceptors_count(self):
        interceptors = get_server_interceptors()
        assert len(interceptors) == 1

    def test_server_interceptor_type(self):
        import grpc.aio

        interceptors = get_server_interceptors()
        assert isinstance(interceptors[0], grpc.aio.ServerInterceptor)

    def test_server_interceptor_has_tracer(self):
        interceptors = get_server_interceptors()
        assert interceptors[0]._tracer is not None


class TestServerInterceptorSpans:
    """Tests for SERVER span creation based on feature flag."""

    @pytest.mark.asyncio
    @patch("lib.grpc_tracing._rpc_spans_enabled", return_value=False)
    async def test_no_spans_when_flag_disabled(self, mock_flag):
        """When flag is off, no SERVER spans are created — context-only."""
        interceptor = get_server_interceptors()[0]

        # Create a mock handler
        mock_handler = MagicMock()
        mock_handler.unary_unary = AsyncMock(return_value="response")
        mock_handler.unary_stream = None
        mock_handler.stream_unary = None
        mock_handler.stream_stream = None
        mock_handler.request_deserializer = None
        mock_handler.response_serializer = None

        mock_details = MagicMock()
        mock_details.method = "/pkg.Svc/Method"

        continuation = AsyncMock(return_value=mock_handler)

        with patch("lib.grpc_tracing.trace") as mock_trace:
            await interceptor.intercept_service(continuation, mock_details)
            # Tracer should NOT start a span (only context propagation)
            mock_trace.get_tracer.return_value.start_as_current_span.assert_not_called()

    @pytest.mark.asyncio
    @patch("lib.grpc_tracing._rpc_spans_enabled", return_value=True)
    async def test_server_span_created_when_flag_enabled(self, mock_flag):
        """When flag is on, SERVER span is created with RPC attributes."""
        interceptor = get_server_interceptors()[0]

        mock_handler = MagicMock()
        mock_handler.unary_unary = AsyncMock(return_value="response")
        mock_handler.unary_stream = None
        mock_handler.stream_unary = None
        mock_handler.stream_stream = None
        mock_handler.request_deserializer = None
        mock_handler.response_serializer = None

        mock_details = MagicMock()
        mock_details.method = "/joustmania.GameCoordinator/StartGame"

        continuation = AsyncMock(return_value=mock_handler)

        result = await interceptor.intercept_service(continuation, mock_details)
        # Result should be a new handler wrapping the original
        assert result is not None


class TestStreamCloseFromOtherContext:
    """#1769: closing a wrapped stream from another task must not log a detach ERROR."""

    @staticmethod
    async def _wrapped_handler(kind: str, rpc_spans: bool):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        async def inner(*_args):
            for i in range(10):
                yield i

        mock_handler = MagicMock()
        mock_handler.unary_unary = None
        mock_handler.unary_stream = inner if kind == "unary_stream" else None
        mock_handler.stream_unary = None
        mock_handler.stream_stream = inner if kind == "stream_stream" else None
        mock_handler.request_deserializer = None
        mock_handler.response_serializer = None
        mock_details = MagicMock()
        mock_details.method = "/pkg.Svc/Stream"

        interceptor = get_server_interceptors()[0]
        interceptor._tracer = provider.get_tracer("test")
        with patch("lib.grpc_tracing._rpc_spans_enabled", return_value=rpc_spans):
            wrapped = await interceptor.intercept_service(AsyncMock(return_value=mock_handler), mock_details)
        return getattr(wrapped, kind), exporter

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["stream_stream", "unary_stream"])
    @pytest.mark.parametrize("rpc_spans", [True, False])
    async def test_close_from_other_task_logs_no_detach_error(self, kind, rpc_spans, caplog):
        handler, exporter = await self._wrapped_handler(kind, rpc_spans)
        context = MagicMock()
        context.invocation_metadata.return_value = []
        gen = handler(MagicMock(), context)

        async def first_item():
            return await gen.__anext__()

        assert await asyncio.create_task(first_item()) == 0

        async def close():
            await gen.aclose()

        with caplog.at_level(logging.DEBUG, logger="opentelemetry.context"):
            await asyncio.create_task(close())

        errors = [r for r in caplog.records if r.name == "opentelemetry.context" and r.levelno >= logging.ERROR]
        assert errors == []
        if rpc_spans:
            spans = exporter.get_finished_spans()
            assert [s.name for s in spans] == ["Svc/Stream"]


class TestParseAddress:
    """Tests for _parse_address in grpc_utils."""

    def test_standard_host_port(self):
        from lib.grpc_utils import _parse_address

        host, port = _parse_address("game-coordinator:50053")
        assert host == "game-coordinator"
        assert port == 50053

    def test_localhost(self):
        from lib.grpc_utils import _parse_address

        host, port = _parse_address("localhost:50052")
        assert host == "localhost"
        assert port == 50052

    def test_ipv6_brackets(self):
        from lib.grpc_utils import _parse_address

        host, port = _parse_address("[::1]:50052")
        assert host == "::1"
        assert port == 50052

    def test_unix_socket(self):
        from lib.grpc_utils import _parse_address

        host, port = _parse_address("unix:/var/run/grpc.sock")
        assert host == "unix:/var/run/grpc.sock"
        assert port is None

    def test_empty_string(self):
        from lib.grpc_utils import _parse_address

        host, port = _parse_address("")
        assert host == ""
        assert port is None

    def test_host_only(self):
        from lib.grpc_utils import _parse_address

        host, port = _parse_address("game-coordinator")
        assert host == "game-coordinator"
        assert port is None


class TestClientInterceptorSpans:
    """Tests for CLIENT span creation based on feature flag."""

    def test_client_interceptor_stores_address(self):
        from lib.grpc_tracing import ContextPropagationInterceptor

        interceptor = ContextPropagationInterceptor("game-coordinator", 50053)
        assert interceptor._server_address == "game-coordinator"
        assert interceptor._server_port == 50053

    def test_client_interceptor_no_address(self):
        from lib.grpc_tracing import ContextPropagationInterceptor

        interceptor = ContextPropagationInterceptor()
        assert interceptor._server_address is None
        assert interceptor._server_port is None


class TestStreamSpanScoping:
    """#1769 review: the rpc span is current only while the handler runs, never in the caller."""

    @staticmethod
    async def _wrap(inner, kind: str = "stream_stream"):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        mock_handler = MagicMock()
        mock_handler.unary_unary = None
        mock_handler.unary_stream = inner if kind == "unary_stream" else None
        mock_handler.stream_unary = None
        mock_handler.stream_stream = inner if kind == "stream_stream" else None
        mock_handler.request_deserializer = None
        mock_handler.response_serializer = None
        mock_details = MagicMock()
        mock_details.method = "/pkg.Svc/Stream"

        interceptor = get_server_interceptors()[0]
        interceptor._tracer = provider.get_tracer("test")
        with patch("lib.grpc_tracing._rpc_spans_enabled", return_value=True):
            wrapped = await interceptor.intercept_service(AsyncMock(return_value=mock_handler), mock_details)
        context = MagicMock()
        context.invocation_metadata.return_value = []
        gen = getattr(wrapped, kind)(MagicMock(), context)
        return gen, exporter, provider.get_tracer("handler")

    @staticmethod
    def _rpc_span(exporter):
        return next(s for s in exporter.get_finished_spans() if s.name == "Svc/Stream")

    @pytest.mark.asyncio
    async def test_child_span_after_yield_parents_to_rpc_span(self):
        tracer_holder = {}

        async def inner(*_args):
            for i in range(3):
                yield i
                with tracer_holder["tracer"].start_as_current_span(f"child{i}"):
                    pass

        gen, exporter, tracer = await self._wrap(inner)
        tracer_holder["tracer"] = tracer
        async for _ in gen:
            pass

        spans = {s.name: s for s in exporter.get_finished_spans()}
        rpc = spans["Svc/Stream"]
        for i in range(3):
            assert spans[f"child{i}"].parent.span_id == rpc.context.span_id
        assert rpc.status.status_code == StatusCode.OK

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["stream_stream", "unary_stream"])
    async def test_rpc_span_not_current_in_caller_between_steps(self, kind):
        async def inner(*_args):
            for i in range(3):
                yield i

        gen, _exporter, _tracer = await self._wrap(inner, kind)
        async for _ in gen:
            assert not trace.get_current_span().get_span_context().is_valid

    @pytest.mark.asyncio
    async def test_handler_span_held_across_yield_loses_parent_after_yield(self):
        """Pins the documented limitation of _iterate_in_context."""
        tracer_holder = {}

        async def inner(*_args):
            with tracer_holder["tracer"].start_as_current_span("outer"):
                yield 1
                with tracer_holder["tracer"].start_as_current_span("after_yield"):
                    pass
                yield 2

        gen, exporter, tracer = await self._wrap(inner)
        tracer_holder["tracer"] = tracer
        async for _ in gen:
            pass

        spans = {s.name: s for s in exporter.get_finished_spans()}
        assert spans["after_yield"].parent.span_id == self._rpc_span(exporter).context.span_id

    @pytest.mark.asyncio
    async def test_handler_exception_sets_error_and_ends_span_once(self):
        async def inner(*_args):
            yield 1
            raise RuntimeError("boom")

        gen, exporter, _tracer = await self._wrap(inner)
        with pytest.raises(RuntimeError):
            async for _ in gen:
                pass

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.ERROR
        assert len(spans[0].events) == 1  # one "exception" event from record_exception

    @pytest.mark.asyncio
    async def test_task_cancel_ends_span_once_unset(self):
        release = asyncio.Event()

        async def inner(*_args):
            yield 1
            await release.wait()
            yield 2

        gen, exporter, _tracer = await self._wrap(inner)

        async def consume():
            async for _ in gen:
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.UNSET

    @pytest.mark.asyncio
    async def test_athrow_sets_error_and_inner_finally_runs_in_rpc_context(self):
        inner_saw_rpc_span = []

        async def inner(*_args):
            try:
                for i in range(10):
                    yield i
            finally:
                inner_saw_rpc_span.append(trace.get_current_span().get_span_context().is_valid)

        gen, exporter, _tracer = await self._wrap(inner)
        await gen.__anext__()
        with pytest.raises(ValueError):
            await gen.athrow(ValueError("x"))

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.ERROR
        assert inner_saw_rpc_span == [True]

    @pytest.mark.asyncio
    async def test_early_break_then_aclose_ends_span_once_unset(self):
        inner_saw_rpc_span = []

        async def inner(*_args):
            try:
                for i in range(10):
                    yield i
            finally:
                inner_saw_rpc_span.append(trace.get_current_span().get_span_context().is_valid)

        gen, exporter, _tracer = await self._wrap(inner)
        async for i in gen:
            if i == 2:
                break
        await gen.aclose()

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.UNSET
        assert inner_saw_rpc_span == [True]


# --- Client spans end at the call's final status (#1879) -----------------------------------------
# A loopback server with generic raw-bytes handlers behind the real create_channel interceptor order
# (metrics, then tracing): a mock continuation hides that grpc returns the call before the RPC ends.

_PROBE_SVC = "test.Probe"
_PROBE = f"/{_PROBE_SVC}/"
_STREAM_FAIL_DELAY_S = 0.2


async def _probe_unary_ok(_request, context):
    _seen_metadata.append(dict(context.invocation_metadata()))
    return b"ok"


async def _probe_unary_fail(_request, context):
    await context.abort(grpc.StatusCode.UNAVAILABLE, "down")


async def _probe_unary_slow(_request, _context):
    await asyncio.sleep(30)
    return b"late"


async def _probe_stream_ok(_request, context):
    _seen_metadata.append(dict(context.invocation_metadata()))
    for _ in range(3):
        yield b"m"


async def _probe_stream_fail(_request, context):
    yield b"m"
    await asyncio.sleep(_STREAM_FAIL_DELAY_S)
    await context.abort(grpc.StatusCode.UNAVAILABLE, "dropped mid-stream")


async def _probe_stream_server_cancel(_request, context):
    yield b"m"
    await context.abort(grpc.StatusCode.CANCELLED, "server cancelled")


async def _probe_stream_forever(_request, _context):
    while True:
        yield b"m"
        await asyncio.sleep(0.01)


async def _probe_stream_many(_request, _context):
    for _ in range(1000):
        yield b"m"


async def _probe_bidi_fail(request_iterator, context):
    async for request in request_iterator:
        yield request
        await context.abort(grpc.StatusCode.INTERNAL, "bidi broke")


async def _probe_bidi_echo(request_iterator, context):
    _seen_metadata.append(dict(context.invocation_metadata()))
    async for request in request_iterator:
        yield request


_seen_metadata: list[dict] = []


def _sent_parent_id(metadata: dict) -> int:
    """The span id in the ``traceparent`` the server received."""
    return int(metadata["traceparent"].split("-")[2], 16)


def _sent_trace_id(metadata: dict) -> int:
    return int(metadata["traceparent"].split("-")[1], 16)


@pytest.fixture
def span_exporter():
    """A local provider (no global one) and the in-memory exporter behind it."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter, provider
    provider.shutdown()


@pytest_asyncio.fixture
async def traced_channel(span_exporter):
    _exporter, provider = span_exporter
    _seen_metadata.clear()
    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                _PROBE_SVC,
                {
                    "UnaryOk": grpc.unary_unary_rpc_method_handler(_probe_unary_ok),
                    "UnaryFail": grpc.unary_unary_rpc_method_handler(_probe_unary_fail),
                    "UnarySlow": grpc.unary_unary_rpc_method_handler(_probe_unary_slow),
                    "StreamOk": grpc.unary_stream_rpc_method_handler(_probe_stream_ok),
                    "StreamFail": grpc.unary_stream_rpc_method_handler(_probe_stream_fail),
                    "StreamServerCancel": grpc.unary_stream_rpc_method_handler(_probe_stream_server_cancel),
                    "StreamForever": grpc.unary_stream_rpc_method_handler(_probe_stream_forever),
                    "StreamMany": grpc.unary_stream_rpc_method_handler(_probe_stream_many),
                    "BidiFail": grpc.stream_stream_rpc_method_handler(_probe_bidi_fail),
                    "BidiEcho": grpc.stream_stream_rpc_method_handler(_probe_bidi_echo),
                },
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    tracing = get_context_propagation_interceptors("127.0.0.1", port)
    for interceptor in tracing:
        interceptor._tracer = provider.get_tracer("client")
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}", interceptors=get_metrics_interceptors() + tracing)
    with patch("lib.grpc_tracing._rpc_spans_enabled", autospec=True, return_value=True):
        yield channel
    await channel.close()
    await server.stop(None)


async def _finished_spans(exporter: InMemorySpanExporter, count: int = 1):
    """Wait until ``count`` spans ended, then give a second end a chance to show up."""
    deadline = asyncio.get_running_loop().time() + 5.0
    while len(exporter.get_finished_spans()) < count:
        assert asyncio.get_running_loop().time() < deadline, "client span never ended"
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    return exporter.get_finished_spans()


def _code(span) -> int:
    return span.attributes["rpc.grpc.status_code"]


@pytest.mark.asyncio
@pytest.mark.uc("UC-035", flow="main", steps=[5])
class TestClientSpanStatusAtCallEnd:
    """CLIENT spans record the final status when the call ends (telemetry.md §3.1, #1879)."""

    async def test_unary_error_status_is_error(self, traced_channel, span_exporter):
        exporter, _ = span_exporter
        with pytest.raises(grpc.aio.AioRpcError):
            await traced_channel.unary_unary(_PROBE + "UnaryFail")(b"x")

        (span,) = await _finished_spans(exporter)
        assert span.name == "Probe/UnaryFail"
        assert span.kind == trace.SpanKind.CLIENT
        assert span.status.status_code == StatusCode.ERROR
        assert _code(span) == grpc.StatusCode.UNAVAILABLE.value[0]

    async def test_unary_ok_is_ok(self, traced_channel, span_exporter):
        exporter, _ = span_exporter
        assert await traced_channel.unary_unary(_PROBE + "UnaryOk")(b"x") == b"ok"

        (span,) = await _finished_spans(exporter)
        assert span.status.status_code == StatusCode.OK
        assert _code(span) == 0

    async def test_unary_task_cancel_propagates_and_leaves_status_unset(self, traced_channel, span_exporter):
        exporter, _ = span_exporter

        async def slow_call():
            return await traced_channel.unary_unary(_PROBE + "UnarySlow")(b"x")

        task = asyncio.create_task(slow_call())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        (span,) = await _finished_spans(exporter)
        assert span.status.status_code == StatusCode.UNSET

    async def test_stream_failing_mid_way_is_error_and_lasts_the_stream(self, traced_channel, span_exporter):
        exporter, _ = span_exporter
        received = []
        with pytest.raises(grpc.aio.AioRpcError):
            async for message in traced_channel.unary_stream(_PROBE + "StreamFail")(b"x"):
                received.append(message)

        assert received == [b"m"]
        (span,) = await _finished_spans(exporter)
        assert span.status.status_code == StatusCode.ERROR
        assert _code(span) == grpc.StatusCode.UNAVAILABLE.value[0]
        assert (span.end_time - span.start_time) / 1e9 >= _STREAM_FAIL_DELAY_S * 0.9

    async def test_stream_ok_is_ok(self, traced_channel, span_exporter):
        exporter, _ = span_exporter
        assert [m async for m in traced_channel.unary_stream(_PROBE + "StreamOk")(b"x")] == [b"m"] * 3

        (span,) = await _finished_spans(exporter)
        assert span.status.status_code == StatusCode.OK
        assert _code(span) == 0

    async def test_server_sent_cancelled_is_error(self, traced_channel, span_exporter):
        exporter, _ = span_exporter
        with pytest.raises(grpc.aio.AioRpcError):
            async for _ in traced_channel.unary_stream(_PROBE + "StreamServerCancel")(b"x"):
                pass

        (span,) = await _finished_spans(exporter)
        assert span.status.status_code == StatusCode.ERROR
        assert _code(span) == grpc.StatusCode.CANCELLED.value[0]

    async def test_bidi_failure_is_error(self, traced_channel, span_exporter):
        exporter, _ = span_exporter

        async def requests():
            yield b"a"
            yield b"b"

        with pytest.raises(grpc.aio.AioRpcError):
            async for _ in traced_channel.stream_stream(_PROBE + "BidiFail")(requests()):
                pass

        (span,) = await _finished_spans(exporter)
        assert span.name == "Probe/BidiFail"
        assert span.status.status_code == StatusCode.ERROR
        assert _code(span) == grpc.StatusCode.INTERNAL.value[0]

    async def test_caller_cancel_ends_span_once_unset(self, traced_channel, span_exporter, caplog):
        exporter, _ = span_exporter
        call = traced_channel.unary_stream(_PROBE + "StreamForever")(b"x")
        await call.read()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call.read()

        spans = await _finished_spans(exporter)
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.UNSET
        assert _code(spans[0]) == grpc.StatusCode.CANCELLED.value[0]
        assert "ended span" not in caplog.text

    async def test_stream_never_read_ends_on_explicit_cancel(self, traced_channel, span_exporter):
        """A stream the caller never reads ends its span once the caller cancels it."""
        exporter, _ = span_exporter
        call = traced_channel.unary_stream(_PROBE + "StreamForever")(b"x")
        await call.initial_metadata()
        call.cancel()

        spans = await _finished_spans(exporter)
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.UNSET
        assert _code(spans[0]) == grpc.StatusCode.CANCELLED.value[0]

    async def test_stream_never_read_and_dropped_stays_live_until_channel_close(self, traced_channel, span_exporter):
        """Dropping a never-read stream does NOT cancel it (grpcio >= 1.83; 1.82 cancelled on gc).

        The RPC stays live server-side, so its CLIENT span stays open: code that abandons a
        stream must cancel it explicitly (call-site audit: #2266). Closing the channel ends
        both. If this starts failing because the span ends on drop, grpcio went back to
        cancelling on gc: re-check #2266.
        """
        exporter, _ = span_exporter
        call = traced_channel.unary_stream(_PROBE + "StreamForever")(b"x")
        await call.initial_metadata()
        del call
        gc.collect()

        await asyncio.sleep(0.5)
        assert exporter.get_finished_spans() == (), "dropped stream was cancelled on gc"

        await traced_channel.close()
        spans = await _finished_spans(exporter)
        assert len(spans) == 1
        assert _code(spans[0]) == grpc.StatusCode.CANCELLED.value[0]

    async def test_long_stream_finishes_span_once_not_per_message(self, traced_channel, span_exporter):
        from lib.grpc_tracing import _ClientSpanFinisher

        exporter, _ = span_exporter
        with patch.object(
            _ClientSpanFinisher, "finish", autospec=True, side_effect=_ClientSpanFinisher.finish
        ) as finish:
            count = 0
            async for _ in traced_channel.unary_stream(_PROBE + "StreamMany")(b"x"):
                count += 1
            await _finished_spans(exporter)

        assert count == 1000
        assert finish.call_count == 1

    async def test_client_span_parents_to_caller_and_traceparent_carries_client_span(
        self, traced_channel, span_exporter
    ):
        """The CLIENT span is the caller's child, and the server receives the CLIENT span (#1897)."""
        exporter, provider = span_exporter
        with provider.get_tracer("caller").start_as_current_span("caller") as outer:
            await traced_channel.unary_unary(_PROBE + "UnaryOk")(b"x")

        spans = {s.name: s for s in await _finished_spans(exporter, 2)}
        outer_id = outer.get_span_context().span_id
        client = spans["Probe/UnaryOk"]
        assert client.parent.span_id == outer_id
        assert _sent_parent_id(_seen_metadata[0]) == client.context.span_id != outer_id
        assert _sent_trace_id(_seen_metadata[0]) == outer.get_span_context().trace_id

    async def _assert_stream_sends_client_span(self, exporter, provider, open_stream) -> None:
        with provider.get_tracer("caller").start_as_current_span("caller") as outer:
            await open_stream()

        spans = {s.name: s for s in await _finished_spans(exporter, 2)}
        client = next(s for s in spans.values() if s.kind == trace.SpanKind.CLIENT)
        assert client.parent.span_id == outer.get_span_context().span_id
        assert _sent_parent_id(_seen_metadata[0]) == client.context.span_id
        assert _sent_trace_id(_seen_metadata[0]) == outer.get_span_context().trace_id

    async def test_server_stream_traceparent_carries_client_span(self, traced_channel, span_exporter):
        exporter, provider = span_exporter

        async def open_stream():
            assert [m async for m in traced_channel.unary_stream(_PROBE + "StreamOk")(b"x")] == [b"m"] * 3

        await self._assert_stream_sends_client_span(exporter, provider, open_stream)

    async def test_bidi_traceparent_carries_client_span(self, traced_channel, span_exporter):
        exporter, provider = span_exporter

        async def requests():
            yield b"a"
            yield b"b"

        async def open_stream():
            assert [m async for m in traced_channel.stream_stream(_PROBE + "BidiEcho")(requests())] == [b"a", b"b"]

        await self._assert_stream_sends_client_span(exporter, provider, open_stream)

    async def test_removed_env_var_creates_no_client_span(self, span_exporter, monkeypatch):
        """OTEL_GRPC_CLIENT_SPANS is gone (#1907): with the flag off no CLIENT span is created."""
        exporter, provider = span_exporter
        monkeypatch.setenv("OTEL_GRPC_CLIENT_SPANS", "true")
        importlib.reload(grpc_tracing)
        try:
            interceptor = grpc_tracing.ContextPropagationInterceptor("127.0.0.1", 1)
            interceptor._tracer = provider.get_tracer("client")
            finished_call = asyncio.get_running_loop().create_future()
            finished_call.set_result(b"ok")
            continuation = AsyncMock(return_value=finished_call)
            details = grpc.aio.ClientCallDetails(_PROBE + "UnaryOk", None, None, None, None)
            with patch.object(grpc_tracing, "_rpc_spans_enabled", autospec=True, return_value=False):
                await interceptor.intercept_unary_unary(continuation, details, b"x")
        finally:
            monkeypatch.undo()
            importlib.reload(grpc_tracing)

        continuation.assert_awaited_once()
        assert exporter.get_finished_spans() == ()

    async def test_no_span_when_flag_off(self, traced_channel, span_exporter):
        """Flag off: no CLIENT span, and the caller's span is sent as before (#1897)."""
        exporter, provider = span_exporter
        with (
            patch.object(grpc_tracing._rpc_spans_enabled, "return_value", False),
            provider.get_tracer("caller").start_as_current_span("caller") as outer,
        ):
            await traced_channel.unary_unary(_PROBE + "UnaryOk")(b"x")
            [_ async for _ in traced_channel.unary_stream(_PROBE + "StreamOk")(b"x")]

        await asyncio.sleep(0.05)
        assert [s.name for s in exporter.get_finished_spans()] == ["caller"]
        outer_id = outer.get_span_context().span_id
        assert [_sent_parent_id(md) for md in _seen_metadata] == [outer_id, outer_id]


@pytest.mark.uc("UC-035", flow="main", steps=[5])
class TestAioDoneCallbackOffLoop:
    """The aio done callback fired by GC off the loop thread still ends the span once (#1899 review)."""

    def _finished_call(self, code=grpc.StatusCode.CANCELLED, details="Cancelled upon garbage collection!"):
        call = MagicMock()
        call.code = AsyncMock(return_value=code)
        call.details = AsyncMock(return_value=details)
        return call

    def test_off_loop_callback_schedules_status_read_on_the_calls_loop(self, span_exporter):
        from lib.grpc_tracing import _ClientSpanFinisher

        exporter, provider = span_exporter
        loop = asyncio.new_event_loop()
        try:
            finisher = _ClientSpanFinisher(provider.get_tracer("t").start_span("P/F"), loop)
            worker = threading.Thread(
                target=finisher.on_aio_call_done, args=(self._finished_call(grpc.StatusCode.UNAVAILABLE, "down"),)
            )
            worker.start()
            worker.join()
            loop.run_until_complete(asyncio.sleep(0.05))
        finally:
            loop.close()

        # UNAVAILABLE is a code the fallback never records: only the call's own status read yields it.
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.ERROR
        assert _code(span) == grpc.StatusCode.UNAVAILABLE.value[0]

    def test_closed_loop_records_cancelled_and_ends_once(self, span_exporter):
        from lib.grpc_tracing import _ClientSpanFinisher

        exporter, provider = span_exporter
        loop = asyncio.new_event_loop()
        loop.close()
        finisher = _ClientSpanFinisher(provider.get_tracer("t").start_span("P/F"), loop)
        finisher.on_aio_call_done(self._finished_call())
        finisher.on_aio_call_done(self._finished_call())

        (span,) = exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.UNSET
        assert _code(span) == grpc.StatusCode.CANCELLED.value[0]


def _sync_probe_ok(_request, context):
    _seen_metadata.append(dict(context.invocation_metadata()))
    return b"ok"


def _sync_probe_fail(_request, context):
    context.abort(grpc.StatusCode.UNAVAILABLE, "down")


def _sync_probe_bidi_echo(request_iterator, context):
    _seen_metadata.append(dict(context.invocation_metadata()))
    yield from request_iterator


@pytest.fixture
def traced_sync_channel(span_exporter):
    _exporter, provider = span_exporter
    _seen_metadata.clear()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                _PROBE_SVC,
                {
                    "UnaryOk": grpc.unary_unary_rpc_method_handler(_sync_probe_ok),
                    "UnaryFail": grpc.unary_unary_rpc_method_handler(_sync_probe_fail),
                    "BidiEcho": grpc.stream_stream_rpc_method_handler(_sync_probe_bidi_echo),
                },
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.intercept_channel(
        grpc.insecure_channel(f"127.0.0.1:{port}"), *get_sync_context_propagation_interceptors("127.0.0.1", port)
    )
    with (
        patch("lib.grpc_tracing._rpc_spans_enabled", autospec=True, return_value=True),
        patch("lib.grpc_tracing.trace.get_tracer", autospec=True, return_value=provider.get_tracer("sync")),
    ):
        yield channel
    channel.close()
    server.stop(None)


@pytest.mark.uc("UC-035", flow="main", steps=[5])
class TestSyncClientSpanStatus:
    """The HID adapters' sync unary calls: grpc's continuation never raises, the outcome carries the status."""

    def test_blocking_unary_error_is_error(self, traced_sync_channel, span_exporter):
        exporter, _ = span_exporter
        with pytest.raises(grpc.RpcError):
            traced_sync_channel.unary_unary(_PROBE + "UnaryFail")(b"x")

        (span,) = exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.ERROR
        assert _code(span) == grpc.StatusCode.UNAVAILABLE.value[0]

    def test_blocking_unary_ok_is_ok(self, traced_sync_channel, span_exporter):
        exporter, _ = span_exporter
        assert traced_sync_channel.unary_unary(_PROBE + "UnaryOk")(b"x") == b"ok"

        (span,) = exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.OK
        assert _code(span) == 0

    def test_blocking_unary_traceparent_carries_client_span(self, traced_sync_channel, span_exporter):
        """rust-hid / python-hid server spans nest under the adapter's CLIENT span (#1897)."""
        exporter, provider = span_exporter
        with provider.get_tracer("caller").start_as_current_span("caller") as outer:
            traced_sync_channel.unary_unary(_PROBE + "UnaryOk")(b"x")

        client = next(s for s in exporter.get_finished_spans() if s.kind == trace.SpanKind.CLIENT)
        assert client.parent.span_id == outer.get_span_context().span_id
        assert _sent_parent_id(_seen_metadata[0]) == client.context.span_id
        assert _sent_trace_id(_seen_metadata[0]) == outer.get_span_context().trace_id

    def test_sync_stream_sends_caller_span_without_client_span(self, traced_sync_channel, span_exporter):
        """The sync StreamIO stream has no CLIENT span but still sends the caller's span (#1897)."""
        exporter, provider = span_exporter
        with provider.get_tracer("caller").start_as_current_span("caller") as outer:
            replies = list(traced_sync_channel.stream_stream(_PROBE + "BidiEcho")(iter([b"a", b"b"])))

        assert replies == [b"a", b"b"]
        assert [s.name for s in exporter.get_finished_spans()] == ["caller"]
        assert _sent_parent_id(_seen_metadata[0]) == outer.get_span_context().span_id
        assert _sent_trace_id(_seen_metadata[0]) == outer.get_span_context().trace_id

    def test_blocking_unary_flag_off_sends_caller_span(self, traced_sync_channel, span_exporter):
        exporter, provider = span_exporter
        with (
            patch.object(grpc_tracing._rpc_spans_enabled, "return_value", False),
            provider.get_tracer("caller").start_as_current_span("caller") as outer,
        ):
            traced_sync_channel.unary_unary(_PROBE + "UnaryOk")(b"x")

        assert [s.name for s in exporter.get_finished_spans()] == ["caller"]
        assert _sent_parent_id(_seen_metadata[0]) == outer.get_span_context().span_id

    def test_future_unary_error_is_error(self, traced_sync_channel, span_exporter):
        exporter, _ = span_exporter
        future = traced_sync_channel.unary_unary(_PROBE + "UnaryFail").future(b"x")
        with pytest.raises(grpc.RpcError):
            future.result(timeout=5)

        deadline = time.monotonic() + 5
        while not exporter.get_finished_spans():
            assert time.monotonic() < deadline, "sync client span never ended"
            time.sleep(0.01)
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.ERROR


# --- Server span status (#1896) -------------------------------------------------------------

_server_handler_cancelled = asyncio.Event()


async def _srv_unary_abort(_request, context):
    await context.abort(grpc.StatusCode.UNAVAILABLE, "down")


async def _srv_unary_set_code(_request, context):
    context.set_code(grpc.StatusCode.NOT_FOUND)
    context.set_details("no such thing")
    return b""


async def _srv_unary_ok(_request, _context):
    return b"ok"


async def _srv_unary_slow(_request, _context):
    try:
        await asyncio.sleep(30)
    finally:
        _server_handler_cancelled.set()


async def _srv_stream_abort(_request, context):
    yield b"m"
    await context.abort(grpc.StatusCode.UNAVAILABLE, "dropped mid-stream")


async def _srv_stream_set_code(_request, context):
    yield b"m"
    context.set_code(grpc.StatusCode.NOT_FOUND)


async def _srv_stream_ok(_request, _context):
    yield b"m"


async def _srv_stream_forever(_request, _context):
    try:
        while True:
            yield b"m"
            await asyncio.sleep(0.01)
    finally:
        _server_handler_cancelled.set()


async def _srv_bidi_abort(request_iterator, context):
    async for request in request_iterator:
        yield request
        await context.abort(grpc.StatusCode.INTERNAL, "bidi broke")


async def _srv_stream_context_write(_request, context):
    await context.write(b"w1")
    await context.write(b"w2")


async def _srv_stream_context_write_abort(_request, context):
    await context.write(b"w1")
    await context.abort(grpc.StatusCode.UNAVAILABLE, "write then abort")


async def _srv_bidi_context_rw(_request_iterator, context):
    while (request := await context.read()) is not grpc.aio.EOF:
        await context.write(request + b"!")


_handler_tracer: list = []  # the test's tracer, for handlers that open their own child span


async def _srv_stream_context_write_child_span(_request, context):
    await context.write(b"w1")
    with _handler_tracer[0].start_as_current_span("handler.child"):
        pass
    await context.write(b"w2")


async def _srv_bidi_context_read_blocks(_request_iterator, context):
    try:
        await context.read()  # the client never writes
    finally:
        _server_handler_cancelled.set()


async def _srv_unary_downstream(_request, _context):
    channel = grpc.aio.insecure_channel("127.0.0.1:1")  # nothing listens here
    try:
        await channel.unary_unary("/x.Y/Z")(b"", timeout=0.5)
    finally:
        await channel.close()


async def _srv_client_stream_abort(request_iterator, context):
    async for _ in request_iterator:
        await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "not now")


@pytest_asyncio.fixture
async def server_traced(span_exporter):
    """A loopback aio server behind ``ServerUnaryUnaryInterceptor``; a plain client channel (no client spans)."""
    _exporter, provider = span_exporter
    _server_handler_cancelled.clear()
    interceptor = grpc_tracing.ServerUnaryUnaryInterceptor()
    interceptor._tracer = provider.get_tracer("server")
    server = grpc.aio.server(interceptors=[interceptor])
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                _PROBE_SVC,
                {
                    "UnaryAbort": grpc.unary_unary_rpc_method_handler(_srv_unary_abort),
                    "UnarySetCode": grpc.unary_unary_rpc_method_handler(_srv_unary_set_code),
                    "UnaryOk": grpc.unary_unary_rpc_method_handler(_srv_unary_ok),
                    "UnarySlow": grpc.unary_unary_rpc_method_handler(_srv_unary_slow),
                    "UnaryDownstream": grpc.unary_unary_rpc_method_handler(_srv_unary_downstream),
                    "ClientStreamAbort": grpc.stream_unary_rpc_method_handler(_srv_client_stream_abort),
                    "StreamAbort": grpc.unary_stream_rpc_method_handler(_srv_stream_abort),
                    "StreamSetCode": grpc.unary_stream_rpc_method_handler(_srv_stream_set_code),
                    "StreamOk": grpc.unary_stream_rpc_method_handler(_srv_stream_ok),
                    "StreamForever": grpc.unary_stream_rpc_method_handler(_srv_stream_forever),
                    "BidiAbort": grpc.stream_stream_rpc_method_handler(_srv_bidi_abort),
                    "StreamContextWrite": grpc.unary_stream_rpc_method_handler(_srv_stream_context_write),
                    "StreamContextWriteAbort": grpc.unary_stream_rpc_method_handler(_srv_stream_context_write_abort),
                    "BidiContextRw": grpc.stream_stream_rpc_method_handler(_srv_bidi_context_rw),
                    "StreamContextWriteChild": grpc.unary_stream_rpc_method_handler(
                        _srv_stream_context_write_child_span
                    ),
                    "BidiContextReadBlocks": grpc.stream_stream_rpc_method_handler(_srv_bidi_context_read_blocks),
                },
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    with patch("lib.grpc_tracing._rpc_spans_enabled", autospec=True, return_value=True):
        yield channel
    await channel.close()
    await server.stop(None)


@pytest.mark.asyncio
@pytest.mark.uc("UC-035", flow="main", steps=[5])
class TestServerContextWriteHandlers:
    """Streaming handlers that use ``context.write`` / ``context.read`` work behind the interceptor (#1929)."""

    async def test_unary_stream_context_write_delivers_and_records_ok(self, server_traced, span_exporter):
        exporter, _ = span_exporter
        received = [m async for m in server_traced.unary_stream(_PROBE + "StreamContextWrite")(b"x")]
        assert received == [b"w1", b"w2"]
        spans = await _finished_spans(exporter)
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.OK
        assert _code(spans[0]) == 0

    async def test_unary_stream_context_write_abort_records_the_abort_code(self, server_traced, span_exporter):
        exporter, _ = span_exporter
        call = server_traced.unary_stream(_PROBE + "StreamContextWriteAbort")(b"x")
        with pytest.raises(grpc.aio.AioRpcError) as err:
            async for _ in call:
                pass
        assert err.value.code() == grpc.StatusCode.UNAVAILABLE
        spans = await _finished_spans(exporter)
        assert spans[0].status.status_code == StatusCode.ERROR
        assert _code(spans[0]) == grpc.StatusCode.UNAVAILABLE.value[0]

    async def test_stream_stream_context_read_and_write(self, server_traced, span_exporter):
        exporter, _ = span_exporter
        call = server_traced.stream_stream(_PROBE + "BidiContextRw")()
        await call.write(b"a")
        assert await call.read() == b"a!"
        await call.write(b"b")
        assert await call.read() == b"b!"
        await call.done_writing()
        assert await call.read() == grpc.aio.EOF
        spans = await _finished_spans(exporter)
        assert spans[0].status.status_code == StatusCode.OK
        assert _code(spans[0]) == 0

    async def test_context_write_handler_child_span_parents_to_the_rpc_span(self, server_traced, span_exporter):
        exporter, provider = span_exporter
        _handler_tracer[:] = [provider.get_tracer("handler")]
        received = [m async for m in server_traced.unary_stream(_PROBE + "StreamContextWriteChild")(b"x")]
        assert received == [b"w1", b"w2"]
        spans = await _finished_spans(exporter, count=2)
        child = next(s for s in spans if s.name == "handler.child")
        rpc = next(s for s in spans if s.kind == trace.SpanKind.SERVER)
        assert child.parent is not None
        assert child.parent.span_id == rpc.context.span_id
        assert child.context.trace_id == rpc.context.trace_id

    async def test_context_write_handler_with_rpc_spans_disabled_delivers_without_a_span(
        self, server_traced, span_exporter
    ):
        exporter, _ = span_exporter
        with patch("lib.grpc_tracing._rpc_spans_enabled", return_value=False):
            received = [m async for m in server_traced.unary_stream(_PROBE + "StreamContextWrite")(b"x")]
        assert received == [b"w1", b"w2"]
        await asyncio.sleep(0.1)
        assert exporter.get_finished_spans() == ()

    async def test_context_read_handler_client_cancel_unblocks_the_handler_and_ends_the_span(
        self, server_traced, span_exporter
    ):
        exporter, _ = span_exporter
        call = server_traced.stream_stream(_PROBE + "BidiContextReadBlocks")()
        await asyncio.sleep(0.1)  # let the handler block in context.read()
        call.cancel()
        deadline = asyncio.get_running_loop().time() + 5.0
        while not _server_handler_cancelled.is_set():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        # grpc ends a cancelled ``context.read()`` without raising, so the span closes with the handler.
        assert len(await _finished_spans(exporter)) == 1


@pytest.mark.asyncio
@pytest.mark.uc("UC-035", flow="main", steps=[5])
class TestServerSpanStatus:
    """SERVER spans record the real gRPC code (telemetry.md §3.1, #1896)."""

    async def _one(self, exporter):
        spans = await _finished_spans(exporter)
        assert len(spans) == 1
        assert spans[0].kind == trace.SpanKind.SERVER
        return spans[0]

    async def test_unary_abort_records_the_abort_code(self, server_traced, span_exporter):
        exporter, _ = span_exporter
        with pytest.raises(grpc.aio.AioRpcError) as err:
            await server_traced.unary_unary(_PROBE + "UnaryAbort")(b"x")
        assert err.value.code() == grpc.StatusCode.UNAVAILABLE  # the abort is not swallowed
        span = await self._one(exporter)
        assert span.status.status_code == StatusCode.ERROR
        assert _code(span) == grpc.StatusCode.UNAVAILABLE.value[0]

    async def test_unary_set_code_and_return_is_error(self, server_traced, span_exporter):
        exporter, _ = span_exporter
        with pytest.raises(grpc.aio.AioRpcError) as err:
            await server_traced.unary_unary(_PROBE + "UnarySetCode")(b"x")
        assert err.value.code() == grpc.StatusCode.NOT_FOUND
        span = await self._one(exporter)
        assert span.status.status_code == StatusCode.ERROR
        assert _code(span) == grpc.StatusCode.NOT_FOUND.value[0]

    async def test_unary_abort_status_description_is_the_details_text(self, server_traced, span_exporter):
        exporter, _ = span_exporter
        with pytest.raises(grpc.aio.AioRpcError):
            await server_traced.unary_unary(_PROBE + "UnaryAbort")(b"x")
        span = await self._one(exporter)
        assert span.status.description == "down"

    async def test_unary_downstream_error_is_unknown_with_one_exception_event(self, server_traced, span_exporter):
        exporter, _ = span_exporter
        with pytest.raises(grpc.aio.AioRpcError) as err:
            await server_traced.unary_unary(_PROBE + "UnaryDownstream")(b"x")
        assert err.value.code() == grpc.StatusCode.UNKNOWN  # grpc does not forward the downstream code
        span = await self._one(exporter)
        assert span.status.status_code == StatusCode.ERROR
        assert _code(span) == grpc.StatusCode.UNKNOWN.value[0]
        assert [event.name for event in span.events] == ["exception"]

    async def test_client_stream_abort_records_the_abort_code(self, server_traced, span_exporter):
        exporter, _ = span_exporter
        call = server_traced.stream_unary(_PROBE + "ClientStreamAbort")()
        await call.write(b"a")
        with pytest.raises(grpc.aio.AioRpcError) as err:
            await call
        assert err.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        span = await self._one(exporter)
        assert span.status.status_code == StatusCode.ERROR
        assert _code(span) == grpc.StatusCode.FAILED_PRECONDITION.value[0]

    @pytest.mark.parametrize("kind", ["unary_unary", "stream_unary"])
    async def test_handler_cancel_is_reraised_not_swallowed(self, span_exporter, kind):
        exporter, provider = span_exporter
        interceptor = grpc_tracing.ServerUnaryUnaryInterceptor()
        interceptor._tracer = provider.get_tracer("server")

        async def slow(_request, _context):
            await asyncio.sleep(30)

        factory = getattr(grpc, f"{kind}_rpc_method_handler")
        inner = factory(slow)

        async def continuation(_details):
            return inner

        details = MagicMock(spec=grpc.HandlerCallDetails)
        details.method = _PROBE + "Slow"
        context = MagicMock()
        context.invocation_metadata.return_value = ()
        context.code.return_value = None
        context.details.return_value = None
        with patch("lib.grpc_tracing._rpc_spans_enabled", autospec=True, return_value=True):
            handler = await interceptor.intercept_service(continuation, details)
        wrapped = getattr(handler, kind)
        task = asyncio.ensure_future(wrapped(b"x", context))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        span = await self._one(exporter)
        assert _code(span) == grpc.StatusCode.CANCELLED.value[0]
        assert span.status.status_code == StatusCode.UNSET

    async def test_unary_ok_is_ok(self, server_traced, span_exporter):
        exporter, _ = span_exporter
        assert await server_traced.unary_unary(_PROBE + "UnaryOk")(b"x") == b"ok"
        span = await self._one(exporter)
        assert span.status.status_code == StatusCode.OK
        assert _code(span) == 0

    async def test_unary_client_cancel_sets_cancelled_and_leaves_status_unset(self, server_traced, span_exporter):
        exporter, _ = span_exporter
        call = server_traced.unary_unary(_PROBE + "UnarySlow")(b"x")
        await asyncio.sleep(0.1)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        span = await self._one(exporter)
        assert _code(span) == grpc.StatusCode.CANCELLED.value[0]
        assert span.status.status_code == StatusCode.UNSET
        assert _server_handler_cancelled.is_set()

    async def test_stream_abort_mid_stream_records_the_abort_code(self, server_traced, span_exporter):
        exporter, _ = span_exporter
        call = server_traced.unary_stream(_PROBE + "StreamAbort")(b"x")
        assert await call.read() == b"m"
        with pytest.raises(grpc.aio.AioRpcError):
            await call.read()
        span = await self._one(exporter)
        assert span.status.status_code == StatusCode.ERROR
        assert _code(span) == grpc.StatusCode.UNAVAILABLE.value[0]

    async def test_stream_set_code_then_end_is_error(self, server_traced, span_exporter):
        exporter, _ = span_exporter
        call = server_traced.unary_stream(_PROBE + "StreamSetCode")(b"x")
        with pytest.raises(grpc.aio.AioRpcError):
            async for _ in call:
                pass
        span = await self._one(exporter)
        assert span.status.status_code == StatusCode.ERROR
        assert _code(span) == grpc.StatusCode.NOT_FOUND.value[0]

    async def test_stream_ok_is_ok(self, server_traced, span_exporter):
        exporter, _ = span_exporter
        async for _ in server_traced.unary_stream(_PROBE + "StreamOk")(b"x"):
            pass
        span = await self._one(exporter)
        assert span.status.status_code == StatusCode.OK
        assert _code(span) == 0

    async def test_stream_client_cancel_sets_cancelled_and_leaves_status_unset(self, server_traced, span_exporter):
        exporter, _ = span_exporter
        call = server_traced.unary_stream(_PROBE + "StreamForever")(b"x")
        await call.read()
        call.cancel()
        deadline = asyncio.get_running_loop().time() + 5.0
        while not _server_handler_cancelled.is_set():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        span = await self._one(exporter)
        assert span.status.status_code == StatusCode.UNSET
        assert _code(span) == grpc.StatusCode.CANCELLED.value[0]

    async def test_bidi_abort_records_the_abort_code(self, server_traced, span_exporter):
        exporter, _ = span_exporter
        call = server_traced.stream_stream(_PROBE + "BidiAbort")()
        await call.write(b"a")
        assert await call.read() == b"a"
        with pytest.raises(grpc.aio.AioRpcError):
            await call.read()
        span = await self._one(exporter)
        assert span.status.status_code == StatusCode.ERROR
        assert _code(span) == grpc.StatusCode.INTERNAL.value[0]


def test_parse_method_health_short_form_matches_collector_filter():
    """#2286: tests/contract/test_collector_best_effort.py hard-codes this form (no lib deps there)."""
    assert grpc_tracing._parse_method("/grpc.health.v1.Health/Check") == ("Health", "Check")
