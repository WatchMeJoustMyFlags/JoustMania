"""
Unit tests for backend_factory.py -- backend selection logic and flagd integration.

Tests the two-level priority: OpenFeature flag > platform detection.
All backends are created as MultiplexerBackend with adapters.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from services.controller_manager.backend_factory import (
    _create_adapter_by_name,
    _create_bt_discovery,
    _resolve_backend_name,
    create_backend,
)


def _mock_details(value, reason="STATIC", error_code=None, error_message=None):
    """Create a mock FlagEvaluationDetails object."""
    details = MagicMock()
    details.value = value
    details.reason = reason
    details.error_code = error_code
    details.error_message = error_message
    return details


class TestResolveBackendName:
    """Test _resolve_backend_name priority logic."""

    def test_openfeature_flag_used(self):
        """OpenFeature flag should be used as primary selection."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("hidapi")

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            result = _resolve_backend_name()
            assert result == "hidapi"

    def test_openfeature_flag_case_insensitive(self):
        """Flag value should be lowercased."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("Mock")

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            result = _resolve_backend_name()
            assert result == "mock"

    def test_returns_none_when_flag_fails(self):
        """Should return None for platform detection when flag evaluation fails."""
        with patch("lib.feature_flags.get_flag_client", side_effect=Exception("flagd unavailable")):
            result = _resolve_backend_name()
            assert result is None

    def test_returns_none_when_flag_returns_empty(self):
        """Empty flag value should fall through to platform detection."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("")

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            result = _resolve_backend_name()
            assert result is None


class TestCreateAdapterByName:
    """Test _create_adapter_by_name adapter factory method."""

    def test_mock_adapter(self):
        adapter = _create_adapter_by_name("mock")
        assert adapter.__class__.__name__ == "MockAdapter"
        assert adapter.adapter_type == "mock"

    def test_unknown_adapter_raises(self):
        with pytest.raises(RuntimeError, match="Unknown adapter"):
            _create_adapter_by_name("nonexistent")


class TestCreateBackendIntegration:
    """Test create_backend end-to-end — always returns MultiplexerBackend."""

    def test_openfeature_selects_mock(self):
        """OpenFeature flag 'mock' should create MultiplexerBackend with mock adapter."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock")

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            backend = create_backend()
            assert backend.__class__.__name__ == "MultiplexerBackend"
            assert len(backend.adapters) == 1
            assert backend.adapters[0].adapter_type == "mock"

    def test_defaults_to_hidapi_when_flag_fails(self):
        """Should default to hidapi when flag evaluation fails."""
        with (
            patch("lib.feature_flags.get_flag_client", side_effect=Exception("flagd unavailable")),
            patch("services.controller_manager.backend_factory._create_adapter_by_name") as mock_create,
        ):
            hidapi_adapter = MagicMock()
            hidapi_adapter.adapter_type = "hidapi"
            mock_adapter = MagicMock()
            mock_adapter.adapter_type = "mock"
            mock_create.side_effect = [hidapi_adapter, mock_adapter]

            backend = create_backend()

        assert backend.__class__.__name__ == "MultiplexerBackend"
        call_names = [call[0][0] for call in mock_create.call_args_list]
        assert "hidapi" in call_names
        assert "mock" in call_names


class TestMultiAdapterCreation:
    """Test comma-separated backend flag with multiplexer."""

    def test_duplicate_backend_names_rejected(self):
        """flag='mock,mock' -> ValueError."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock,mock")

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            pytest.raises(ValueError, match="Duplicate backend names"),
        ):
            create_backend()

    def test_mock_hidapi_creates_two_adapters(self):
        """flag='mock,hidapi' -> MultiplexerBackend with 2 adapters."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock,hidapi")

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch("services.controller_manager.backend_factory._create_adapter_by_name") as mock_create,
        ):
            mock_adapter = MagicMock()
            mock_adapter.adapter_type = "mock"
            hidapi_adapter = MagicMock()
            hidapi_adapter.adapter_type = "hidapi"
            mock_create.side_effect = [mock_adapter, hidapi_adapter]

            backend = create_backend()

        assert backend.__class__.__name__ == "MultiplexerBackend"
        assert len(backend.adapters) == 2
        call_names = [call[0][0] for call in mock_create.call_args_list]
        assert "mock" in call_names
        assert "hidapi" in call_names

    def test_single_name_creates_adapter(self):
        """flag='mock' -> MultiplexerBackend with 1 adapter."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock")

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            backend = create_backend()

        assert backend.__class__.__name__ == "MultiplexerBackend"
        assert len(backend.adapters) == 1
        assert backend.adapters[0].adapter_type == "mock"

    def test_whitespace_in_comma_separated_is_trimmed(self):
        """flag='mock , hidapi' -> names trimmed properly."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock , hidapi")

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch("services.controller_manager.backend_factory._create_adapter_by_name") as mock_create,
        ):
            mock_adapter = MagicMock()
            mock_adapter.adapter_type = "mock"
            hidapi_adapter = MagicMock()
            hidapi_adapter.adapter_type = "hidapi"
            mock_create.side_effect = [mock_adapter, hidapi_adapter]

            backend = create_backend()

        assert backend.__class__.__name__ == "MultiplexerBackend"
        call_names = [call[0][0] for call in mock_create.call_args_list]
        assert "mock" in call_names
        assert "hidapi" in call_names


class TestBTDiscoveryInjection:
    """Test CentralizedBTDiscovery creation and injection."""

    def test_hidapi_gets_hidapi_discovery(self):
        from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery

        discovery = _create_bt_discovery(["hidapi"])
        assert isinstance(discovery, CentralizedBTDiscovery)
        assert discovery.discovery_mode == "hidapi"

    def test_mock_gets_no_discovery(self):
        discovery = _create_bt_discovery(["mock"])
        assert discovery is None

    def test_mock_hidapi_gets_hidapi_discovery(self):
        from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery

        discovery = _create_bt_discovery(["mock", "hidapi"])
        assert isinstance(discovery, CentralizedBTDiscovery)
        assert discovery.discovery_mode == "hidapi"

    def test_multiplexer_receives_bt_discovery(self):
        """MultiplexerBackend should hold bt_discovery when hidapi adapter used."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock,hidapi")

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch("services.controller_manager.backend_factory._create_adapter_by_name") as mock_create,
        ):
            mock_adapter = MagicMock()
            mock_adapter.adapter_type = "mock"
            hidapi_adapter = MagicMock()
            hidapi_adapter.adapter_type = "hidapi"
            mock_create.side_effect = [mock_adapter, hidapi_adapter]

            backend = create_backend()

        assert backend.__class__.__name__ == "MultiplexerBackend"
        assert backend.bt_discovery is not None
        assert backend.bt_discovery.discovery_mode == "hidapi"

    def test_mock_only_receives_no_discovery(self):
        """Mock-only should pass bt_discovery=None."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock")

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            backend = create_backend()

        assert backend.bt_discovery is None
