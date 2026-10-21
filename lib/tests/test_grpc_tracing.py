"""Tests for lib/grpc_tracing.py"""

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

# Add lib to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from lib.grpc_tracing import (
    _build_rpc_attributes,
    _extract_method_name,
    _parse_method,
    _prepare_metadata,
    _rpc_spans_enabled,
    _safe_detach,
    get_context_propagation_interceptors,
    get_server_interceptors,
)


class TestExtractMethodName:
    """Tests for _extract_method_name helper."""

    def test_string_with_slash(self):
        result = _extract_method_name("/package.Service/Method")
        assert result == "package.Service/Method"

    def test_string_without_slash(self):
        result = _extract_method_name("Method")
        assert result == "Method"

    def test_bytes_input(self):
        result = _extract_method_name(b"/pkg.Svc/Method")
        assert result == "pkg.Svc/Method"

    def test_bytes_without_slash(self):
        result = _extract_method_name(b"Method")
        assert result == "Method"


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
