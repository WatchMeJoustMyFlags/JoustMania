"""Tests for FlagdLogLevelFilter."""

from unittest.mock import MagicMock, patch

from lib.flagd_log_processor import FlagdLogLevelFilter

PATCH_TARGET = "lib.feature_flags.get_flag_client"


def _make_log_record(severity_number_value: int) -> MagicMock:
    """Create a mock ReadableLogRecord with the given severity number."""
    record = MagicMock()
    severity = MagicMock()
    severity.value = severity_number_value
    record.severity_number = severity
    return record


def _make_log_record_no_severity() -> MagicMock:
    """Create a mock ReadableLogRecord with severity_number=None."""
    record = MagicMock()
    record.severity_number = None
    return record


class TestFlagdLogLevelFilter:
    def setup_method(self):
        self.delegate = MagicMock()
        self.filter = FlagdLogLevelFilter(self.delegate)

    @patch(PATCH_TARGET)
    def test_passes_info_when_not_verbose(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_boolean_value.return_value = False
        mock_get_client.return_value = mock_client

        record = _make_log_record(9)  # INFO
        self.filter.on_emit(record)
        self.delegate.on_emit.assert_called_once()

    @patch(PATCH_TARGET)
    def test_drops_debug_when_not_verbose(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_boolean_value.return_value = False
        mock_get_client.return_value = mock_client

        record = _make_log_record(5)  # DEBUG
        self.filter.on_emit(record)
        self.delegate.on_emit.assert_not_called()

    @patch(PATCH_TARGET)
    def test_passes_debug_when_verbose(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_boolean_value.return_value = True
        mock_get_client.return_value = mock_client

        record = _make_log_record(5)  # DEBUG
        self.filter.on_emit(record)
        self.delegate.on_emit.assert_called_once()

    @patch(PATCH_TARGET, side_effect=Exception("flagd down"))
    def test_drops_debug_when_flagd_unavailable(self, _mock):
        record = _make_log_record(5)  # DEBUG
        self.filter.on_emit(record)
        self.delegate.on_emit.assert_not_called()

    @patch(PATCH_TARGET)
    def test_passes_through_when_severity_number_is_none(self, mock_get_client):
        """Records with no severity_number set should always pass through."""
        mock_client = MagicMock()
        mock_client.get_boolean_value.return_value = False
        mock_get_client.return_value = mock_client

        record = _make_log_record_no_severity()
        self.filter.on_emit(record)
        self.delegate.on_emit.assert_called_once()

    def test_delegates_shutdown(self):
        self.filter.shutdown()
        self.delegate.shutdown.assert_called_once()

    def test_delegates_force_flush(self):
        self.filter.force_flush(5000)
        self.delegate.force_flush.assert_called_once_with(5000)
