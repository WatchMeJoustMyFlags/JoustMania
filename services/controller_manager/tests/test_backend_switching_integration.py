"""Integration tests for backend switching with Rust adapter.

These tests verify that the backend factory correctly creates and
routes to the Rust adapter when configured, without requiring
the actual rust-hid service (uses mocked gRPC stubs).
"""

import queue
import threading
from unittest.mock import MagicMock, patch

import grpc

from services.controller_manager.backend_factory import (
    _create_adapter_by_name,
    create_backend,
)
from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend
from services.controller_manager.multiplexer.rust_adapter import RustServiceAdapter


def _mock_details(value, reason="STATIC", error_code=None, error_message=None):
    """Create a mock FlagEvaluationDetails object."""
    details = MagicMock()
    details.value = value
    details.reason = reason
    details.error_code = error_code
    details.error_message = error_message
    return details


def _make_rust_adapter():
    """Create a RustServiceAdapter bypassing __init__ gRPC imports."""
    adapter = RustServiceAdapter.__new__(RustServiceAdapter)
    adapter._channel = MagicMock()
    adapter._stub = MagicMock()
    adapter._command_queue = queue.Queue(maxsize=256)
    adapter._latest_data = {}
    adapter._last_state = {}
    adapter._data_lock = threading.Lock()
    adapter._stream_thread = None
    adapter._stream_running = False
    adapter._adapter_names = {}
    return adapter


class TestFactoryCreatesRustAdapter:
    """Verify BackendFactory creates MultiplexerBackend with RustServiceAdapter when flag is 'rust'."""

    def test_factory_creates_rust_adapter(self):
        """flag='rust' -> MultiplexerBackend with RustServiceAdapter + MockAdapter."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("rust")

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch("services.controller_manager.backend_factory._create_adapter_by_name") as mock_create,
        ):
            rust_adapter = _make_rust_adapter()
            mock_adapter = MagicMock()
            mock_adapter.adapter_type = "mock"
            mock_create.side_effect = [rust_adapter, mock_adapter]

            backend = create_backend()

        assert isinstance(backend, MultiplexerBackend)
        assert len(backend.adapters) == 2
        adapter_types = {a.adapter_type for a in backend.adapters}
        assert "rust" in adapter_types
        assert "mock" in adapter_types

    def test_direct_adapter_creation_returns_rust_type(self):
        """_create_adapter_by_name('rust') creates RustServiceAdapter with correct adapter_type."""
        with (
            patch("proto.psmove_hid_pb2_grpc.ControllerIOServiceStub"),
            patch("grpc.insecure_channel", return_value=MagicMock()),
        ):
            adapter = _create_adapter_by_name("rust")

        assert isinstance(adapter, RustServiceAdapter)
        assert adapter.adapter_type == "rust"


class TestFactoryCreatesMixedAdapters:
    """Verify 'mock,rust' creates both adapters in the multiplexer."""

    def test_comma_separated_mock_rust(self):
        """flag='mock,rust' -> MultiplexerBackend with both adapters."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock,rust")

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch("services.controller_manager.backend_factory._create_adapter_by_name") as mock_create,
        ):
            mock_adapter = MagicMock()
            mock_adapter.adapter_type = "mock"
            rust_adapter = _make_rust_adapter()
            mock_create.side_effect = [mock_adapter, rust_adapter]

            backend = create_backend()

        assert isinstance(backend, MultiplexerBackend)
        assert len(backend.adapters) == 2
        call_names = [call[0][0] for call in mock_create.call_args_list]
        assert "mock" in call_names
        assert "rust" in call_names


class TestRustAdapterGracefulDegradation:
    """When rust-hid service is unreachable, discover() returns empty list (not crash)."""

    def test_discover_returns_empty_on_rpc_error(self):
        """RPC failure during discover() returns empty list instead of raising."""
        adapter = _make_rust_adapter()
        adapter._stub.Discover.side_effect = grpc.RpcError()
        adapter._stream_running = True

        serials = adapter.discover()

        assert serials == []
        assert isinstance(serials, list)

    def test_open_returns_false_on_rpc_error(self):
        """RPC failure during open() returns False instead of raising."""
        adapter = _make_rust_adapter()
        adapter._stub.Open.side_effect = grpc.RpcError()
        adapter._stream_running = True

        result = adapter.open("AABBCCDDEEFF")

        assert result is False

    def test_close_does_not_raise_on_rpc_error(self):
        """RPC failure during close() is swallowed (logged, not raised)."""
        adapter = _make_rust_adapter()
        adapter._stub.Close.side_effect = grpc.RpcError()

        # Should not raise
        adapter.close("AABBCCDDEEFF")

    def test_set_output_returns_false_when_stream_not_running(self):
        """set_output() returns False when the bidir stream is not active."""
        adapter = _make_rust_adapter()
        adapter._stream_running = False

        result = adapter.set_output("AABBCCDDEEFF", 255, 0, 0, 100)

        assert result is False


class TestBackendSwitchHidapiToRust:
    """Simulate flag change from 'python' to 'rust', verify backend is recreated."""

    def test_backend_switch_produces_different_adapter_types(self):
        """Switching flag from 'python' to 'rust' produces different adapter types."""
        # First creation: python
        mock_client_python = MagicMock()
        mock_client_python.get_string_details.return_value = _mock_details("python")

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client_python),
            patch("services.controller_manager.backend_factory._create_adapter_by_name") as mock_create,
        ):
            python_adapter = MagicMock()
            python_adapter.adapter_type = "python"
            mock_adapter_1 = MagicMock()
            mock_adapter_1.adapter_type = "mock"
            mock_create.side_effect = [python_adapter, mock_adapter_1]

            backend_1 = create_backend()

        # Second creation: rust
        mock_client_rust = MagicMock()
        mock_client_rust.get_string_details.return_value = _mock_details("rust")

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client_rust),
            patch("services.controller_manager.backend_factory._create_adapter_by_name") as mock_create,
        ):
            rust_adapter = _make_rust_adapter()
            mock_adapter_2 = MagicMock()
            mock_adapter_2.adapter_type = "mock"
            mock_create.side_effect = [rust_adapter, mock_adapter_2]

            backend_2 = create_backend()

        # Verify different backends
        assert isinstance(backend_1, MultiplexerBackend)
        assert isinstance(backend_2, MultiplexerBackend)

        types_1 = {a.adapter_type for a in backend_1.adapters}
        types_2 = {a.adapter_type for a in backend_2.adapters}

        assert "python" in types_1
        assert "python" not in types_2
        assert "rust" in types_2
        assert "rust" not in types_1

    def test_mock_auto_injected_in_both_modes(self):
        """Mock adapter is always auto-injected regardless of backend flag."""
        for flag_value in ("python", "rust"):
            mock_client = MagicMock()
            mock_client.get_string_details.return_value = _mock_details(flag_value)

            with (
                patch("lib.feature_flags.get_flag_client", return_value=mock_client),
                patch("services.controller_manager.backend_factory._create_adapter_by_name") as mock_create,
            ):
                primary = MagicMock()
                primary.adapter_type = flag_value
                mock_adapter = MagicMock()
                mock_adapter.adapter_type = "mock"
                mock_create.side_effect = [primary, mock_adapter]

                backend = create_backend()

            adapter_types = {a.adapter_type for a in backend.adapters}
            assert "mock" in adapter_types, f"mock not injected for flag={flag_value}"
