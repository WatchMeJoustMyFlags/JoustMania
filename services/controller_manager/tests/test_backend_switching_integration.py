"""Integration tests for backend switching with Rust adapter.

These tests verify that the backend factory correctly creates and
routes to the Rust adapter when configured, without requiring
the actual rust-hid service (uses mocked gRPC stubs).
"""

import json
import queue
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import grpc
import pytest

from services.controller_manager.backend_factory import (
    DEFAULT_BACKEND,
    _create_adapter_by_name,
    create_backend,
)
from services.controller_manager.multiplexer.adapter import DiscoveryFailedError
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
    adapter._init_stream_state()
    adapter._channel = MagicMock()
    adapter._stub = MagicMock()
    adapter._command_queue = queue.Queue(maxsize=256)
    adapter._latest_data = {}
    adapter._last_state = {}
    adapter._trigger_latch = set()
    adapter._data_lock = threading.Lock()
    adapter._closed = set()
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

    def test_discover_raises_discovery_failed_on_rpc_error(self):
        """RPC failure during discover() is a failed pass, not "no controllers" (UC-002 A1, #2125)."""
        adapter = _make_rust_adapter()
        adapter._stub.Discover.side_effect = grpc.RpcError()
        adapter._stream_running = True

        with pytest.raises(DiscoveryFailedError):
            adapter.discover()

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


_CONTROLLER_FLAGS = Path(__file__).resolve().parents[3] / "services" / "flagd" / "controller.json"


class TestRustIsTheDefaultBackend:
    """rust-hid is the default Bluetooth backend; python is opt-in (UC-002 BR-003, #1371)."""

    @pytest.mark.uc("UC-002", flow="main", steps=[2], br=["BR-003"], cond=["Pre-2"])
    @pytest.mark.parametrize(
        "flag_client",
        [
            pytest.param(lambda: _client_returning(_mock_details("")), id="empty-flag"),
            pytest.param(lambda: _client_raising(RuntimeError("flagd down")), id="flagd-down"),
        ],
    )
    def test_unavailable_flag_falls_back_to_rust_plus_mock(self, flag_client):
        """An empty or failing backend flag loads rust-hid (+ the auto-injected mock), never python."""
        with (
            patch("lib.feature_flags.get_flag_client", return_value=flag_client()),
            patch("services.controller_manager.backend_factory._create_adapter_by_name") as mock_create,
        ):
            mock_adapter = MagicMock()
            mock_adapter.adapter_type = "mock"
            mock_create.side_effect = [_make_rust_adapter(), mock_adapter]

            create_backend()

        assert [call[0][0] for call in mock_create.call_args_list] == [DEFAULT_BACKEND, "mock"]
        assert DEFAULT_BACKEND == "rust"

    @pytest.mark.uc("UC-002", flow="main", steps=[2], br=["BR-003"])
    def test_shipped_flags_default_to_rust_and_keep_python_opt_in(self):
        """controller.json defaults backend and bluetooth_backend to rust; python stays opt-in; no mock_* doubles."""
        flags = json.loads(_CONTROLLER_FLAGS.read_text())["flags"]

        backend = flags["backend"]
        assert backend["variants"][backend["defaultVariant"]] == "rust"
        assert set(backend["variants"]) == {"python", "mock", "rust", "python_rust"}
        # The mock adapter is always added, so no variant needs to name it next to a real backend.
        assert not any(name.startswith("mock_") for name in backend["variants"])
        # Warning and death effect timings are fixed constants, not flags (#1322).
        assert not any(key.startswith("effect.") for key in flags)

        routing = flags["bluetooth_backend"]
        assert routing["variants"][routing["defaultVariant"]] == "rust"
        assert set(routing["variants"].values()) == {"python", "rust", "unstable"}


def _client_returning(details):
    client = MagicMock()
    client.get_string_details.return_value = details
    return client


def _client_raising(exc):
    client = MagicMock()
    client.get_string_details.side_effect = exc
    return client
