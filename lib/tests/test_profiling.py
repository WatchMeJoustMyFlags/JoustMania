"""Tests for lib/profiling.py"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add lib to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import lib.profiling as profiling_module
from lib.profiling import disable_profiling_for_tests, init_profiling


@pytest.fixture(autouse=True)
def _reset_profiling_state():
    """Reset module-level globals between tests."""
    original_initialized = profiling_module._initialized
    original_test_mode = profiling_module._test_mode
    yield
    profiling_module._initialized = original_initialized
    profiling_module._test_mode = original_test_mode


class TestDisableProfiling:
    """Tests for disable_profiling_for_tests."""

    def test_sets_flags(self):
        profiling_module._test_mode = False
        profiling_module._initialized = False

        disable_profiling_for_tests()

        assert profiling_module._test_mode is True
        assert profiling_module._initialized is True


class TestIsProfilingEnabled:
    """Tests for _is_profiling_enabled."""

    @patch("lib.feature_flags.get_flag_client")
    def test_flag_enabled(self, mock_get_client):
        mock_client = mock_get_client.return_value
        mock_client.get_boolean_value.return_value = True

        from lib.profiling import _is_profiling_enabled

        assert _is_profiling_enabled() is True

    @patch("lib.feature_flags.get_flag_client")
    def test_flag_disabled(self, mock_get_client):
        mock_client = mock_get_client.return_value
        mock_client.get_boolean_value.return_value = False

        from lib.profiling import _is_profiling_enabled

        assert _is_profiling_enabled() is False

    @patch("lib.feature_flags.get_flag_client", side_effect=Exception("connection refused"))
    def test_flag_client_error(self, mock_get_client):
        from lib.profiling import _is_profiling_enabled

        assert _is_profiling_enabled() is False


class TestInitProfiling:
    """Tests for init_profiling."""

    def test_already_initialized(self):
        profiling_module._initialized = True

        init_profiling()

        # Should be a no-op, no errors
        assert profiling_module._initialized is True

    @patch("lib.profiling._is_profiling_enabled", return_value=False)
    def test_profiling_disabled(self, mock_enabled):
        profiling_module._initialized = False

        init_profiling()

        assert profiling_module._initialized is True

    @patch("lib.profiling._is_profiling_enabled", return_value=True)
    def test_pyroscope_not_installed(self, mock_enabled):
        profiling_module._initialized = False

        with patch.dict(sys.modules, {"pyroscope": None}):
            init_profiling()

        assert profiling_module._initialized is True

    @patch("lib.profiling._is_profiling_enabled", return_value=False)
    def test_idempotent(self, mock_enabled):
        profiling_module._initialized = False

        init_profiling()
        init_profiling()  # Second call should be no-op

        assert profiling_module._initialized is True
        # _is_profiling_enabled called only once (second call short-circuits)
        mock_enabled.assert_called_once()
