"""Tests for lib/grpc_tracing.py"""

import sys
from pathlib import Path
from unittest.mock import patch

# Add lib to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from lib.grpc_tracing import (
    _extract_method_name,
    _prepare_metadata,
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

    def test_server_interceptors_count(self):
        interceptors = get_server_interceptors()
        assert len(interceptors) == 1

    def test_server_interceptor_type(self):
        import grpc.aio

        interceptors = get_server_interceptors()
        assert isinstance(interceptors[0], grpc.aio.ServerInterceptor)
