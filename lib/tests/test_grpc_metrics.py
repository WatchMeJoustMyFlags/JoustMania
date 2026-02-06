"""Unit tests for the gRPC client metrics interceptors."""

from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import grpc.aio
import pytest

from lib.grpc_metrics import (
    MetricsStreamStreamInterceptor,
    MetricsStreamUnaryInterceptor,
    MetricsUnaryStreamInterceptor,
    MetricsUnaryUnaryInterceptor,
    _parse_method,
    get_metrics_interceptors,
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


class TestMetricsUnaryUnaryInterceptor:
    """Tests for the unary-unary metrics interceptor."""

    @pytest.mark.asyncio
    async def test_records_duration_on_success(self):
        """Test that duration histogram is recorded on a successful call."""
        interceptor = MetricsUnaryUnaryInterceptor()

        # Mock the continuation (the actual gRPC call)
        mock_response = MagicMock()
        continuation = AsyncMock(return_value=mock_response)

        # Mock call details
        call_details = MagicMock(spec=grpc.aio.ClientCallDetails)
        call_details.method = "/pkg.SettingsService/GetSetting"

        request = MagicMock()

        with patch("lib.grpc_metrics.grpc_client_request_duration_seconds") as mock_histogram:
            mock_labeled = MagicMock()
            mock_histogram.labels.return_value = mock_labeled

            result = await interceptor.intercept_unary_unary(continuation, call_details, request)

            assert result == mock_response
            mock_histogram.labels.assert_called_once_with(service="SettingsService", method="GetSetting")
            mock_labeled.observe.assert_called_once()
            # Duration should be a positive float
            observed_value = mock_labeled.observe.call_args[0][0]
            assert observed_value >= 0

    @pytest.mark.asyncio
    async def test_records_error_counter_on_rpc_error(self):
        """Test that error counter is incremented on AioRpcError and exception is re-raised."""
        interceptor = MetricsUnaryUnaryInterceptor()

        # Create a mock AioRpcError
        mock_error = grpc.aio.AioRpcError(
            code=grpc.StatusCode.UNAVAILABLE,
            initial_metadata=grpc.aio.Metadata(),
            trailing_metadata=grpc.aio.Metadata(),
            details="Service unavailable",
            debug_error_string=None,
        )
        continuation = AsyncMock(side_effect=mock_error)

        call_details = MagicMock(spec=grpc.aio.ClientCallDetails)
        call_details.method = "/pkg.GameCoordinator/StartGame"

        request = MagicMock()

        with (
            patch("lib.grpc_metrics.grpc_client_request_duration_seconds") as mock_histogram,
            patch("lib.grpc_metrics.grpc_client_errors_total") as mock_counter,
        ):
            mock_labeled_hist = MagicMock()
            mock_histogram.labels.return_value = mock_labeled_hist
            mock_labeled_counter = MagicMock()
            mock_counter.labels.return_value = mock_labeled_counter

            with pytest.raises(grpc.aio.AioRpcError):
                await interceptor.intercept_unary_unary(continuation, call_details, request)

            # Duration should still be recorded
            mock_histogram.labels.assert_called_once_with(service="GameCoordinator", method="StartGame")
            mock_labeled_hist.observe.assert_called_once()

            # Error counter should be incremented with the status code name
            mock_counter.labels.assert_called_once_with(
                service="GameCoordinator", method="StartGame", code="UNAVAILABLE"
            )
            mock_labeled_counter.inc.assert_called_once()


class TestMetricsUnaryStreamInterceptor:
    """Tests for the unary-stream metrics interceptor."""

    @pytest.mark.asyncio
    async def test_records_duration_on_success(self):
        """Test that duration is recorded for unary-stream calls."""
        interceptor = MetricsUnaryStreamInterceptor()

        mock_response = MagicMock()
        continuation = AsyncMock(return_value=mock_response)

        call_details = MagicMock(spec=grpc.aio.ClientCallDetails)
        call_details.method = "/pkg.MenuService/StreamMenuEvents"

        with patch("lib.grpc_metrics.grpc_client_request_duration_seconds") as mock_histogram:
            mock_labeled = MagicMock()
            mock_histogram.labels.return_value = mock_labeled

            result = await interceptor.intercept_unary_stream(continuation, call_details, MagicMock())

            assert result == mock_response
            mock_histogram.labels.assert_called_once_with(service="MenuService", method="StreamMenuEvents")
            mock_labeled.observe.assert_called_once()


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


class TestMetricsStreamStreamInterceptor:
    """Tests for the stream-stream metrics interceptor."""

    @pytest.mark.asyncio
    async def test_records_duration_on_success(self):
        """Test that duration is recorded for stream-stream calls."""
        interceptor = MetricsStreamStreamInterceptor()

        mock_response = MagicMock()
        continuation = AsyncMock(return_value=mock_response)

        call_details = MagicMock(spec=grpc.aio.ClientCallDetails)
        call_details.method = "/pkg.ControllerManager/StreamGameplayData"

        with patch("lib.grpc_metrics.grpc_client_request_duration_seconds") as mock_histogram:
            mock_labeled = MagicMock()
            mock_histogram.labels.return_value = mock_labeled

            result = await interceptor.intercept_stream_stream(continuation, call_details, MagicMock())

            assert result == mock_response
            mock_histogram.labels.assert_called_once_with(service="ControllerManager", method="StreamGameplayData")
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
