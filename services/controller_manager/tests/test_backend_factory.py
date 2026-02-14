"""
Unit tests for backend_factory.py -- backend selection logic and flagd integration.

Tests the two-level priority: OpenFeature flag > platform detection.
Tests that mock_controller_count is read from flagd
with proper env var fallback when flagd is unavailable.
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
    _create_backend_by_name,
    _create_bt_discovery,
    _get_mock_controller_count,
    _is_multiplexer_enabled,
    _resolve_backend_name,
    create_backend,
)


class TestResolveBackendName:
    """Test _resolve_backend_name priority logic."""

    def test_openfeature_flag_used(self):
        """OpenFeature flag should be used as primary selection."""
        mock_client = MagicMock()
        mock_client.get_string_value.return_value = "hidapi"

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            result = _resolve_backend_name()
            assert result == "hidapi"

    def test_openfeature_flag_case_insensitive(self):
        """Flag value should be lowercased."""
        mock_client = MagicMock()
        mock_client.get_string_value.return_value = "Mock"

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
        mock_client.get_string_value.return_value = ""

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
        # Verify flag name and default
        args = mock_client.get_integer_value.call_args
        assert args[0][0] == "mock_controller_count"
        assert args[0][1] == 4  # default value

    @patch("lib.feature_flags.get_flag_client")
    def test_returns_default_on_flagd_error(self, mock_get_client):
        mock_get_client.side_effect = RuntimeError("flagd unavailable")

        result = _get_mock_controller_count()

        assert result == 4  # hardcoded default


class TestCreateBackendByName:
    """Test _create_backend_by_name factory method."""

    def test_mock_backend(self):
        backend = _create_backend_by_name("mock")
        assert backend.__class__.__name__ == "MockBackend"

    def test_unknown_backend_raises(self):
        with pytest.raises(RuntimeError, match="Unknown backend"):
            _create_backend_by_name("nonexistent")


class TestCreateBackendIntegration:
    """Test create_backend end-to-end."""

    def test_openfeature_selects_mock(self):
        """OpenFeature flag should create the correct backend."""
        mock_client = MagicMock()
        mock_client.get_string_value.return_value = "mock"
        mock_client.get_boolean_value.return_value = False

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            backend = create_backend()
            assert backend.__class__.__name__ == "MockBackend"

    def test_platform_fallback_when_flag_fails(self):
        """Should fall through to platform detection when flag fails."""
        with patch("lib.feature_flags.get_flag_client", side_effect=Exception("flagd unavailable")):
            # Just verify _resolve_backend_name returns None (platform detection)
            result = _resolve_backend_name()
            assert result is None


class TestMultiplexerBackendEnabled:
    """Test multiplexer_backend_enabled flag wrapping."""

    def test_wraps_in_multiplexer_when_flag_enabled(self):
        """When multiplexer flag is on, backend should be wrapped in MultiplexerBackend."""
        mock_client = MagicMock()
        # First call: get_string_value for controller_backend → "mock"
        mock_client.get_string_value.return_value = "mock"
        # Second call: get_boolean_value for multiplexer_backend_enabled → True
        mock_client.get_boolean_value.return_value = True

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            backend = create_backend()

        assert backend.__class__.__name__ == "MultiplexerBackend"
        assert len(backend.children) == 1
        assert backend.children[0].__class__.__name__ == "MockBackend"

    def test_returns_plain_backend_when_flag_disabled(self):
        """When multiplexer flag is off, backend should be returned as-is."""
        mock_client = MagicMock()
        mock_client.get_string_value.return_value = "mock"
        mock_client.get_boolean_value.return_value = False

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            backend = create_backend()

        assert backend.__class__.__name__ == "MockBackend"

    def test_is_multiplexer_enabled_returns_false_on_error(self):
        """Should default to False when flagd is unavailable."""
        with patch("lib.feature_flags.get_flag_client", side_effect=Exception("unavailable")):
            assert _is_multiplexer_enabled() is False


class TestMultiBackendCreation:
    """Test comma-separated backend flag with multiplexer enabled."""

    def test_duplicate_backend_names_rejected(self):
        """flag='mock,mock' with multiplexer on -> ValueError (duplicates not allowed)."""
        mock_client = MagicMock()
        mock_client.get_string_value.return_value = "mock,mock"
        mock_client.get_boolean_value.return_value = True

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            pytest.raises(ValueError, match="Duplicate backend names"),
        ):
            create_backend()

    def test_mock_bluetooth_creates_two_children(self):
        """flag='mock,bluetooth' with multiplexer on -> MultiplexerBackend with 2 children."""
        mock_client = MagicMock()
        mock_client.get_string_value.return_value = "mock,bluetooth"
        mock_client.get_boolean_value.return_value = True

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch("services.controller_manager.backend_factory._create_backend_by_name") as mock_create,
        ):
            mock_backend = MagicMock()
            mock_backend.__class__.__name__ = "MockBackend"
            bt_backend = MagicMock()
            bt_backend.__class__.__name__ = "BluetoothBackend"
            mock_create.side_effect = [mock_backend, bt_backend]

            backend = create_backend()

        assert backend.__class__.__name__ == "MultiplexerBackend"
        assert len(backend.children) == 2
        # bt_discovery is passed as kwarg to both (CentralizedBTDiscovery for bluetooth combo)
        call_names = [call[0][0] for call in mock_create.call_args_list]
        assert "mock" in call_names
        assert "bluetooth" in call_names

    def test_comma_separated_legacy_uses_first_name(self):
        """flag='mock,bluetooth' with multiplexer off -> plain backend from first name."""
        mock_client = MagicMock()
        mock_client.get_string_value.return_value = "mock,bluetooth"
        mock_client.get_boolean_value.return_value = False

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            backend = create_backend()

        # Legacy path uses first name only
        assert backend.__class__.__name__ == "MockBackend"

    def test_invalid_combination_raises(self):
        """flag='bluetooth,hidapi' with multiplexer on -> ValueError."""
        mock_client = MagicMock()
        mock_client.get_string_value.return_value = "bluetooth,hidapi"
        mock_client.get_boolean_value.return_value = True

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            pytest.raises(ValueError, match="Unsupported backend combination"),
        ):
            create_backend()

    def test_single_name_still_wraps(self):
        """flag='mock' with multiplexer on -> MultiplexerBackend with 1 child (Phase 1 behavior)."""
        mock_client = MagicMock()
        mock_client.get_string_value.return_value = "mock"
        mock_client.get_boolean_value.return_value = True

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            backend = create_backend()

        assert backend.__class__.__name__ == "MultiplexerBackend"
        assert len(backend.children) == 1
        assert backend.children[0].__class__.__name__ == "MockBackend"

    def test_whitespace_in_comma_separated_is_trimmed(self):
        """flag='mock , bluetooth' -> names trimmed properly."""
        mock_client = MagicMock()
        mock_client.get_string_value.return_value = "mock , bluetooth"
        mock_client.get_boolean_value.return_value = True

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch("services.controller_manager.backend_factory._create_backend_by_name") as mock_create,
        ):
            mock_be = MagicMock()
            mock_be.__class__.__name__ = "MockBackend"
            bt_be = MagicMock()
            bt_be.__class__.__name__ = "BluetoothBackend"
            mock_create.side_effect = [mock_be, bt_be]

            backend = create_backend()

        assert backend.__class__.__name__ == "MultiplexerBackend"
        mock_create.assert_any_call("mock", bt_discovery=mock_create.call_args_list[0][1].get("bt_discovery"))
        mock_create.assert_any_call("bluetooth", bt_discovery=mock_create.call_args_list[1][1].get("bt_discovery"))


class TestBTDiscoveryInjection:
    """Test CentralizedBTDiscovery creation and injection."""

    def test_bluetooth_gets_discovery(self):
        """_create_bt_discovery returns CentralizedBTDiscovery for bluetooth backends."""
        from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery

        discovery = _create_bt_discovery(["bluetooth"])
        assert isinstance(discovery, CentralizedBTDiscovery)

    def test_mock_gets_no_discovery(self):
        """_create_bt_discovery returns None for non-bluetooth backends."""
        discovery = _create_bt_discovery(["mock"])
        assert discovery is None

    def test_mock_bluetooth_gets_discovery(self):
        """_create_bt_discovery returns CentralizedBTDiscovery when bluetooth is in the list."""
        from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery

        discovery = _create_bt_discovery(["mock", "bluetooth"])
        assert isinstance(discovery, CentralizedBTDiscovery)

    def test_bluetooth_backend_receives_discovery_via_factory(self):
        """When multiplexer+bluetooth, BluetoothBackend should receive bt_discovery."""
        mock_client = MagicMock()
        mock_client.get_string_value.return_value = "mock,bluetooth"
        mock_client.get_boolean_value.return_value = True

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch("services.controller_manager.backend_factory._create_backend_by_name") as mock_create,
        ):
            mock_be = MagicMock()
            mock_be.__class__.__name__ = "MockBackend"
            bt_be = MagicMock()
            bt_be.__class__.__name__ = "BluetoothBackend"
            mock_create.side_effect = [mock_be, bt_be]

            create_backend()

        # bluetooth call should have bt_discovery set (not None)
        bt_call = mock_create.call_args_list[1]
        assert bt_call[1]["bt_discovery"] is not None

    def test_mock_backend_receives_no_discovery(self):
        """Mock-only with multiplexer should pass bt_discovery=None."""
        mock_client = MagicMock()
        mock_client.get_string_value.return_value = "mock"
        mock_client.get_boolean_value.return_value = True

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch("services.controller_manager.backend_factory._create_backend_by_name") as mock_create,
        ):
            mock_be = MagicMock()
            mock_be.__class__.__name__ = "MockBackend"
            mock_create.return_value = mock_be

            create_backend()

        call = mock_create.call_args
        assert call[1]["bt_discovery"] is None
