"""
FlagdLogLevelFilter — Dynamic log verbosity via OpenFeature/flagd.

Wraps a delegate ``LogRecordProcessor`` and drops log records below
INFO severity when the ``verbose_logging`` flag is *off*. When toggled
*on*, all records (including DEBUG) pass through to the exporter.
"""

import logging

from opentelemetry.context import Context
from opentelemetry.sdk._logs import LogRecordProcessor, ReadableLogRecord

logger = logging.getLogger(__name__)


class FlagdLogLevelFilter(LogRecordProcessor):
    """Filters log records based on the flagd verbose_logging flag."""

    def __init__(self, delegate: LogRecordProcessor) -> None:
        self._delegate = delegate

    def on_emit(self, log_data: ReadableLogRecord, context: Context | None = None) -> None:
        if not self._is_verbose():
            severity = log_data.severity_number
            # OTel severity: TRACE=1-4, DEBUG=5-8, INFO=9-12
            if severity is not None and severity.value < 9:
                return  # Drop DEBUG/TRACE when not verbose
        self._delegate.on_emit(log_data, context)

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._delegate.force_flush(timeout_millis)

    # ------------------------------------------------------------------

    @staticmethod
    def _is_verbose() -> bool:
        try:
            from lib.feature_flags import get_flag_client

            client = get_flag_client("performance")
            return client.get_boolean_value("verbose_logging", False)
        except Exception:
            logger.debug("Failed to read verbose_logging from flagd, defaulting to False", exc_info=True)
            return False
