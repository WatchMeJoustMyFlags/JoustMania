"""
Tests for frequency flag listener in servicer.

Tests the direct flagd integration for dynamic streaming frequency changes.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

import services.controller_manager.servicer as servicer_mod


class TestOnSystemFlagsChanged:
    """Tests for _on_system_flags_changed handler."""

    def _reset(self):
        self._original = servicer_mod._frequency_override
        servicer_mod._frequency_override = None

    def _restore(self):
        servicer_mod._frequency_override = self._original

    @patch("services.controller_manager.servicer.metrics")
    @patch("lib.feature_flags.get_flag_client")
    def test_valid_value_updates_override(self, mock_get_client, mock_metrics):
        self._reset()
        try:
            mock_client = MagicMock()
            mock_client.get_integer_value.return_value = 30
            mock_get_client.return_value = mock_client

            servicer_mod._on_system_flags_changed(None)
            assert servicer_mod._frequency_override == 30
            mock_metrics.stream_frequency_changes_total.inc.assert_called_once()
            mock_metrics.stream_current_frequency_hz.set.assert_called_once_with(30)
        finally:
            self._restore()

    @patch("services.controller_manager.servicer.metrics")
    @patch("lib.feature_flags.get_flag_client")
    def test_zero_value_skipped(self, mock_get_client, mock_metrics):
        self._reset()
        try:
            mock_client = MagicMock()
            mock_client.get_integer_value.return_value = 0
            mock_get_client.return_value = mock_client

            servicer_mod._on_system_flags_changed(None)
            assert servicer_mod._frequency_override is None
        finally:
            self._restore()

    @patch("services.controller_manager.servicer.metrics")
    @patch("lib.feature_flags.get_flag_client")
    def test_negative_value_skipped(self, mock_get_client, mock_metrics):
        self._reset()
        try:
            mock_client = MagicMock()
            mock_client.get_integer_value.return_value = -5
            mock_get_client.return_value = mock_client

            servicer_mod._on_system_flags_changed(None)
            assert servicer_mod._frequency_override is None
        finally:
            self._restore()

    @patch("services.controller_manager.servicer.metrics")
    @patch("lib.feature_flags.get_flag_client")
    def test_clamped_to_max(self, mock_get_client, mock_metrics):
        self._reset()
        try:
            mock_client = MagicMock()
            mock_client.get_integer_value.return_value = 500
            mock_get_client.return_value = mock_client

            servicer_mod._on_system_flags_changed(None)
            assert servicer_mod._frequency_override == 100
        finally:
            self._restore()

    @patch("services.controller_manager.servicer.metrics")
    @patch("lib.feature_flags.get_flag_client")
    def test_clamped_to_min(self, mock_get_client, mock_metrics):
        self._reset()
        try:
            mock_client = MagicMock()
            mock_client.get_integer_value.return_value = -1
            mock_get_client.return_value = mock_client

            servicer_mod._on_system_flags_changed(None)
            # Negative values are <= 0, so skipped entirely
            assert servicer_mod._frequency_override is None
        finally:
            self._restore()

    @patch("services.controller_manager.servicer.metrics")
    @patch("lib.feature_flags.get_flag_client")
    def test_same_value_no_metric_update(self, mock_get_client, mock_metrics):
        self._reset()
        servicer_mod._frequency_override = 60
        try:
            mock_client = MagicMock()
            mock_client.get_integer_value.return_value = 60
            mock_get_client.return_value = mock_client

            servicer_mod._on_system_flags_changed(None)
            assert servicer_mod._frequency_override == 60
            mock_metrics.stream_frequency_changes_total.inc.assert_not_called()
        finally:
            self._restore()

    @patch("services.controller_manager.servicer.logger")
    @patch("lib.feature_flags.get_flag_client")
    def test_exception_handled_gracefully(self, mock_get_client, mock_logger):
        self._reset()
        servicer_mod._frequency_override = 60
        try:
            mock_get_client.side_effect = RuntimeError("flagd down")

            servicer_mod._on_system_flags_changed(None)
            assert servicer_mod._frequency_override == 60  # unchanged
            mock_logger.warning.assert_called_once()
        finally:
            self._restore()

    @patch("lib.feature_flags.get_flag_client")
    def test_skipped_when_other_flag_changed(self, mock_get_client):
        """Handler skips evaluation when a different flag changed."""
        self._reset()
        try:
            event = MagicMock()
            event.flags_changed = ["poll_drop_threshold"]

            servicer_mod._on_system_flags_changed(event)
            assert servicer_mod._frequency_override is None
            mock_get_client.assert_not_called()
        finally:
            self._restore()

    @patch("services.controller_manager.servicer.metrics")
    @patch("lib.feature_flags.get_flag_client")
    def test_evaluates_when_our_flag_changed(self, mock_get_client, mock_metrics):
        """Handler evaluates when game_loop.update_frequency_hz is in changed flags."""
        self._reset()
        try:
            mock_client = MagicMock()
            mock_client.get_integer_value.return_value = 45
            mock_get_client.return_value = mock_client

            event = MagicMock()
            event.flags_changed = ["poll_drop_threshold", "game_loop.update_frequency_hz"]

            servicer_mod._on_system_flags_changed(event)
            assert servicer_mod._frequency_override == 45
        finally:
            self._restore()
