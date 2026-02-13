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
    _get_mock_controller_count,
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

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            backend = create_backend()
            assert backend.__class__.__name__ == "MockBackend"

    def test_platform_fallback_when_flag_fails(self):
        """Should fall through to platform detection when flag fails."""
        with patch("lib.feature_flags.get_flag_client", side_effect=Exception("flagd unavailable")):
            # Just verify _resolve_backend_name returns None (platform detection)
            result = _resolve_backend_name()
            assert result is None
