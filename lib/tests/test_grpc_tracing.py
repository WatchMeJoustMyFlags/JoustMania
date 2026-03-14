"""Tests for lib/grpc_tracing.py"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
