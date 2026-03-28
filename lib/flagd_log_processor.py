"""
FlagdLogLevelFilter — Dynamic log verbosity via OpenFeature/flagd.

Wraps a delegate ``LogRecordProcessor`` and drops log records below
INFO severity when the ``verbose_logging`` flag is *off*. When toggled
*on*, all records (including DEBUG) pass through to the exporter.

Flag values are cached for 2 seconds to avoid per-record flagd calls
and prevent a feedback loop when verbose mode increases log volume.
"""

import logging
import time

from opentelemetry.sdk._logs import LogRecordProcessor, ReadableLogRecord

logger = logging.getLogger(__name__)

_CACHE_TTL = 2.0  # seconds


class FlagdLogLevelFilter(LogRecordProcessor):
    """Filters log records based on the flagd verbose_logging flag."""

    def __init__(self, delegate: LogRecordProcessor) -> None:
        self._delegate = delegate
        self._cached_verbose: bool = False
        self._last_fetch: float = 0.0

    def on_emit(self, log_data: ReadableLogRecord) -> None:
        if not self._is_verbose():
            severity = log_data.severity_number
            # OTel severity: TRACE=1-4, DEBUG=5-8, INFO=9-12
            if severity is not None and severity.value < 9:
                return  # Drop DEBUG/TRACE when not verbose
        self._delegate.on_emit(log_data)

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._delegate.force_flush(timeout_millis)

    # ------------------------------------------------------------------

    def _is_verbose(self) -> bool:
        now = time.monotonic()
        if now - self._last_fetch < _CACHE_TTL:
            return self._cached_verbose

        try:
            from lib.feature_flags import get_flag_client

            client = get_flag_client("performance")
            self._cached_verbose = client.get_boolean_value("verbose_logging", False)
            self._last_fetch = now
        except Exception:
            logger.debug("Failed to read verbose_logging from flagd, using cached value", exc_info=True)
        return self._cached_verbose
