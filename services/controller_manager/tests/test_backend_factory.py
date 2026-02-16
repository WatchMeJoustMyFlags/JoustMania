"""
Unit tests for backend_factory.py -- backend selection logic and flagd integration.

Tests the two-level priority: OpenFeature flag > platform detection.
Tests that mock_controller_count is read from flagd.
Tests adapter factory (Phase 4) and legacy backend factory paths.
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
    _create_backend_by_name,
    _create_bt_discovery,
    _get_mock_controller_count,
    _is_multiplexer_enabled,
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


class TestGetMockControllerCount:
    """Tests for _get_mock_controller_count flag reader."""

    @patch("lib.feature_flags.get_flag_client")
    def test_reads_from_flagd(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_integer_value.return_value = 6
        mock_get_client.return_value = mock_client

        result = _get_mock_controller_count()

        assert result == 6
        mock_client.get_integer_value.assert_called_once()
        args = mock_client.get_integer_value.call_args
        assert args[0][0] == "mock_controller_count"
        assert args[0][1] == 4  # default value

    @patch("lib.feature_flags.get_flag_client")
    def test_returns_default_on_flagd_error(self, mock_get_client):
        mock_get_client.side_effect = RuntimeError("flagd unavailable")

        result = _get_mock_controller_count()

        assert result == 4  # hardcoded default


class TestCreateBackendByName:
    """Test _create_backend_by_name legacy factory method."""

    def test_mock_backend(self):
        backend = _create_backend_by_name("mock")
        assert backend.__class__.__name__ == "MockBackend"

    def test_unknown_backend_raises(self):
        with pytest.raises(RuntimeError, match="Unknown backend"):
            _create_backend_by_name("nonexistent")


class TestCreateAdapterByName:
    """Test _create_adapter_by_name adapter factory method (Phase 4)."""

    def test_mock_adapter(self):
        adapter = _create_adapter_by_name("mock")
        assert adapter.__class__.__name__ == "MockAdapter"
        assert adapter.adapter_type == "mock"

    def test_unknown_adapter_raises(self):
        with pytest.raises(RuntimeError, match="Unknown adapter"):
            _create_adapter_by_name("nonexistent")


class TestCreateBackendIntegration:
    """Test create_backend end-to-end."""

    def test_openfeature_selects_mock(self):
        """OpenFeature flag should create the correct backend."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock")
        mock_client.get_boolean_details.return_value = _mock_details(False)

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            backend = create_backend()
            assert backend.__class__.__name__ == "MockBackend"

    def test_platform_fallback_when_flag_fails(self):
        """Should fall through to platform detection when flag fails."""
        with patch("lib.feature_flags.get_flag_client", side_effect=Exception("flagd unavailable")):
            result = _resolve_backend_name()
            assert result is None


class TestMultiplexerBackendEnabled:
    """Test multiplexer_backend_enabled flag — now creates adapters (Phase 4)."""

    def test_creates_adapter_based_multiplexer_when_enabled(self):
        """When multiplexer flag is on, should create MultiplexerBackend with adapters."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock")
        mock_client.get_boolean_details.return_value = _mock_details(True)

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            backend = create_backend()

        assert backend.__class__.__name__ == "MultiplexerBackend"
        assert len(backend.adapters) == 1
        assert backend.adapters[0].adapter_type == "mock"

    def test_returns_plain_backend_when_flag_disabled(self):
        """When multiplexer flag is off, backend should be returned as-is."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock")
        mock_client.get_boolean_details.return_value = _mock_details(False)

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            backend = create_backend()

        assert backend.__class__.__name__ == "MockBackend"

    def test_is_multiplexer_enabled_returns_false_on_error(self):
        """Should default to False when flagd is unavailable."""
        with patch("lib.feature_flags.get_flag_client", side_effect=Exception("unavailable")):
            assert _is_multiplexer_enabled() is False


class TestMultiAdapterCreation:
    """Test comma-separated backend flag with multiplexer enabled (Phase 4)."""

    def test_duplicate_backend_names_rejected(self):
        """flag='mock,mock' with multiplexer on -> ValueError."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock,mock")
        mock_client.get_boolean_details.return_value = _mock_details(True)

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            pytest.raises(ValueError, match="Duplicate backend names"),
        ):
            create_backend()

    def test_mock_bluetooth_creates_two_adapters(self):
        """flag='mock,bluetooth' with multiplexer on -> MultiplexerBackend with 2 adapters."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock,bluetooth")
        mock_client.get_boolean_details.return_value = _mock_details(True)

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch("services.controller_manager.backend_factory._create_adapter_by_name") as mock_create,
        ):
            mock_adapter = MagicMock()
            mock_adapter.adapter_type = "mock"
            bt_adapter = MagicMock()
            bt_adapter.adapter_type = "psmove"
            mock_create.side_effect = [mock_adapter, bt_adapter]

            backend = create_backend()

        assert backend.__class__.__name__ == "MultiplexerBackend"
        assert len(backend.adapters) == 2
        call_names = [call[0][0] for call in mock_create.call_args_list]
        assert "mock" in call_names
        assert "bluetooth" in call_names

    def test_comma_separated_legacy_uses_first_name(self):
        """flag='mock,bluetooth' with multiplexer off -> plain backend from first name."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock,bluetooth")
        mock_client.get_boolean_details.return_value = _mock_details(False)

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            backend = create_backend()

        assert backend.__class__.__name__ == "MockBackend"

    def test_bluetooth_hidapi_creates_three_adapters(self):
        """flag='bluetooth,hidapi' with multiplexer on -> MultiplexerBackend with 3 adapters (+ auto-injected mock)."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("bluetooth,hidapi")
        mock_client.get_boolean_details.return_value = _mock_details(True)

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch("services.controller_manager.backend_factory._create_adapter_by_name") as mock_create,
        ):
            bt_adapter = MagicMock()
            bt_adapter.adapter_type = "psmove"
            hidapi_adapter = MagicMock()
            hidapi_adapter.adapter_type = "hidapi"
            mock_adapter = MagicMock()
            mock_adapter.adapter_type = "mock"
            mock_create.side_effect = [bt_adapter, hidapi_adapter, mock_adapter]

            backend = create_backend()

        assert backend.__class__.__name__ == "MultiplexerBackend"
        assert len(backend.adapters) == 3
        call_names = [call[0][0] for call in mock_create.call_args_list]
        assert "bluetooth" in call_names
        assert "hidapi" in call_names
        assert "mock" in call_names

    def test_single_name_creates_adapter(self):
        """flag='mock' with multiplexer on -> MultiplexerBackend with 1 adapter."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock")
        mock_client.get_boolean_details.return_value = _mock_details(True)

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            backend = create_backend()

        assert backend.__class__.__name__ == "MultiplexerBackend"
        assert len(backend.adapters) == 1
        assert backend.adapters[0].adapter_type == "mock"

    def test_whitespace_in_comma_separated_is_trimmed(self):
        """flag='mock , bluetooth' -> names trimmed properly."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock , bluetooth")
        mock_client.get_boolean_details.return_value = _mock_details(True)

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch("services.controller_manager.backend_factory._create_adapter_by_name") as mock_create,
        ):
            mock_adapter = MagicMock()
            mock_adapter.adapter_type = "mock"
            bt_adapter = MagicMock()
            bt_adapter.adapter_type = "psmove"
            mock_create.side_effect = [mock_adapter, bt_adapter]

            backend = create_backend()

        assert backend.__class__.__name__ == "MultiplexerBackend"
        call_names = [call[0][0] for call in mock_create.call_args_list]
        assert "mock" in call_names
        assert "bluetooth" in call_names


class TestBTDiscoveryInjection:
    """Test CentralizedBTDiscovery creation and injection."""

    def test_bluetooth_gets_bluez_discovery(self):
        from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery

        discovery = _create_bt_discovery(["bluetooth"])
        assert isinstance(discovery, CentralizedBTDiscovery)
        assert discovery.discovery_mode == "bluez"

    def test_hidapi_gets_hidapi_discovery(self):
        from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery

        discovery = _create_bt_discovery(["hidapi"])
        assert isinstance(discovery, CentralizedBTDiscovery)
        assert discovery.discovery_mode == "hidapi"

    def test_mock_gets_no_discovery(self):
        discovery = _create_bt_discovery(["mock"])
        assert discovery is None

    def test_mock_bluetooth_gets_bluez_discovery(self):
        from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery

        discovery = _create_bt_discovery(["mock", "bluetooth"])
        assert isinstance(discovery, CentralizedBTDiscovery)
        assert discovery.discovery_mode == "bluez"

    def test_mock_hidapi_gets_hidapi_discovery(self):
        from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery

        discovery = _create_bt_discovery(["mock", "hidapi"])
        assert isinstance(discovery, CentralizedBTDiscovery)
        assert discovery.discovery_mode == "hidapi"

    def test_multiplexer_receives_bt_discovery(self):
        """MultiplexerBackend should hold bt_discovery when bluetooth adapter used."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock,bluetooth")
        mock_client.get_boolean_details.return_value = _mock_details(True)

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch("services.controller_manager.backend_factory._create_adapter_by_name") as mock_create,
        ):
            mock_adapter = MagicMock()
            mock_adapter.adapter_type = "mock"
            bt_adapter = MagicMock()
            bt_adapter.adapter_type = "psmove"
            mock_create.side_effect = [mock_adapter, bt_adapter]

            backend = create_backend()

        assert backend.__class__.__name__ == "MultiplexerBackend"
        assert backend.bt_discovery is not None
        assert backend.bt_discovery.discovery_mode == "bluez"

    def test_mock_only_receives_no_discovery(self):
        """Mock-only with multiplexer should pass bt_discovery=None."""
        mock_client = MagicMock()
        mock_client.get_string_details.return_value = _mock_details("mock")
        mock_client.get_boolean_details.return_value = _mock_details(True)

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            backend = create_backend()

        assert backend.bt_discovery is None
