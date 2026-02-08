"""
Unit tests for frequency_flag module.

Tests the direct flagd integration for dynamic streaming frequency changes.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from services.controller_manager.frequency_flag import (
    MAX_FREQUENCY_HZ,
    MIN_FREQUENCY_HZ,
    _clamp_frequency,
    _refresh_frequency,
    get_flag_frequency,
)


class TestClampFrequency:
    """Tests for frequency clamping logic."""

    def test_clamp_within_bounds(self):
        """Value within bounds is returned unchanged."""
        assert _clamp_frequency(30) == 30
        assert _clamp_frequency(60) == 60
        assert _clamp_frequency(1) == 1
        assert _clamp_frequency(100) == 100

    def test_clamp_below_minimum(self):
        """Values below minimum are clamped to MIN_FREQUENCY_HZ."""
        assert _clamp_frequency(0) == MIN_FREQUENCY_HZ
        assert _clamp_frequency(-10) == MIN_FREQUENCY_HZ

    def test_clamp_above_maximum(self):
        """Values above maximum are clamped to MAX_FREQUENCY_HZ."""
        assert _clamp_frequency(101) == MAX_FREQUENCY_HZ
        assert _clamp_frequency(1000) == MAX_FREQUENCY_HZ

    def test_clamp_boundary_values(self):
        """Exact boundary values are preserved."""
        assert _clamp_frequency(MIN_FREQUENCY_HZ) == MIN_FREQUENCY_HZ
        assert _clamp_frequency(MAX_FREQUENCY_HZ) == MAX_FREQUENCY_HZ


class TestGetFlagFrequency:
    """Tests for getting the current flag frequency."""

    def test_returns_none_when_uninitialized(self):
        """Returns None when no flag override has been set."""
        import services.controller_manager.frequency_flag as mod

        # Save and reset state
        original = mod._flag_frequency_hz
        mod._flag_frequency_hz = None
        try:
            assert get_flag_frequency() is None
        finally:
            mod._flag_frequency_hz = original

    def test_returns_value_when_set(self):
        """Returns the current override value when set."""
        import services.controller_manager.frequency_flag as mod

        original = mod._flag_frequency_hz
        mod._flag_frequency_hz = 45
        try:
            assert get_flag_frequency() == 45
        finally:
            mod._flag_frequency_hz = original


class TestRefreshFrequency:
    """Tests for the flag refresh logic."""

    def test_no_client_does_nothing(self):
        """When no flag client is set, refresh is a no-op."""
        import services.controller_manager.frequency_flag as mod

        original_client = mod._flag_client
        original_hz = mod._flag_frequency_hz
        mod._flag_client = None
        mod._flag_frequency_hz = None
        try:
            _refresh_frequency()
            assert mod._flag_frequency_hz is None
        finally:
            mod._flag_client = original_client
            mod._flag_frequency_hz = original_hz

    def test_zero_value_skipped(self):
        """Flag value of 0 is treated as unset and skipped."""
        import services.controller_manager.frequency_flag as mod

        mock_client = MagicMock()
        mock_client.get_integer_value.return_value = 0

        original_client = mod._flag_client
        original_hz = mod._flag_frequency_hz
        mod._flag_client = mock_client
        mod._flag_frequency_hz = None
        try:
            _refresh_frequency()
            assert mod._flag_frequency_hz is None
        finally:
            mod._flag_client = original_client
            mod._flag_frequency_hz = original_hz

    def test_negative_value_skipped(self):
        """Negative flag values are skipped."""
        import services.controller_manager.frequency_flag as mod

        mock_client = MagicMock()
        mock_client.get_integer_value.return_value = -5

        original_client = mod._flag_client
        original_hz = mod._flag_frequency_hz
        mod._flag_client = mock_client
        mod._flag_frequency_hz = None
        try:
            _refresh_frequency()
            assert mod._flag_frequency_hz is None
        finally:
            mod._flag_client = original_client
            mod._flag_frequency_hz = original_hz

    @patch("services.controller_manager.frequency_flag.logger")
    def test_valid_value_updates_frequency(self, mock_logger):
        """A valid flag value updates the stored frequency."""
        import services.controller_manager.frequency_flag as mod

        mock_client = MagicMock()
        mock_client.get_integer_value.return_value = 30

        original_client = mod._flag_client
        original_hz = mod._flag_frequency_hz
        mod._flag_client = mock_client
        mod._flag_frequency_hz = None
        try:
            _refresh_frequency()
            assert mod._flag_frequency_hz == 30
        finally:
            mod._flag_client = original_client
            mod._flag_frequency_hz = original_hz

    @patch("services.controller_manager.frequency_flag.logger")
    def test_value_clamped_to_max(self, mock_logger):
        """Flag values above max are clamped."""
        import services.controller_manager.frequency_flag as mod

        mock_client = MagicMock()
        mock_client.get_integer_value.return_value = 500

        original_client = mod._flag_client
        original_hz = mod._flag_frequency_hz
        mod._flag_client = mock_client
        mod._flag_frequency_hz = None
        try:
            _refresh_frequency()
            assert mod._flag_frequency_hz == MAX_FREQUENCY_HZ
        finally:
            mod._flag_client = original_client
            mod._flag_frequency_hz = original_hz

    @patch("services.controller_manager.frequency_flag.logger")
    def test_same_value_no_update(self, mock_logger):
        """When flag value matches current, no update occurs."""
        import services.controller_manager.frequency_flag as mod

        mock_client = MagicMock()
        mock_client.get_integer_value.return_value = 60

        original_client = mod._flag_client
        original_hz = mod._flag_frequency_hz
        mod._flag_client = mock_client
        mod._flag_frequency_hz = 60
        try:
            _refresh_frequency()
            assert mod._flag_frequency_hz == 60
            # Logger should not have been called with update message
            for call in mock_logger.info.call_args_list:
                assert "Streaming frequency flag updated" not in str(call)
        finally:
            mod._flag_client = original_client
            mod._flag_frequency_hz = original_hz

    @patch("services.controller_manager.frequency_flag.logger")
    def test_changed_value_logs_update(self, mock_logger):
        """When flag value changes, an info log is emitted."""
        import services.controller_manager.frequency_flag as mod

        mock_client = MagicMock()
        mock_client.get_integer_value.return_value = 15

        original_client = mod._flag_client
        original_hz = mod._flag_frequency_hz
        mod._flag_client = mock_client
        mod._flag_frequency_hz = 60
        try:
            _refresh_frequency()
            assert mod._flag_frequency_hz == 15
            # Check that update was logged
            log_messages = [str(call) for call in mock_logger.info.call_args_list]
            assert any("60 -> 15 Hz" in msg for msg in log_messages)
        finally:
            mod._flag_client = original_client
            mod._flag_frequency_hz = original_hz

    @patch("services.controller_manager.frequency_flag.logger")
    def test_exception_handled_gracefully(self, mock_logger):
        """Exceptions during flag read are caught and logged."""
        import services.controller_manager.frequency_flag as mod

        mock_client = MagicMock()
        mock_client.get_integer_value.side_effect = RuntimeError("flagd unavailable")

        original_client = mod._flag_client
        original_hz = mod._flag_frequency_hz
        mod._flag_client = mock_client
        mod._flag_frequency_hz = 60
        try:
            _refresh_frequency()
            # Frequency should remain unchanged
            assert mod._flag_frequency_hz == 60
            # Warning should be logged
            mock_logger.warning.assert_called_once()
        finally:
            mod._flag_client = original_client
            mod._flag_frequency_hz = original_hz


class TestInitFrequencyFlag:
    """Tests for initialization logic."""

    @patch("services.controller_manager.frequency_flag._refresh_frequency")
    def test_double_init_skipped(self, mock_refresh):
        """Second call to init is a no-op."""
        import services.controller_manager.frequency_flag as mod

        original = mod._initialized
        mod._initialized = True
        try:
            mod.init_frequency_flag()
            mock_refresh.assert_not_called()
        finally:
            mod._initialized = original
