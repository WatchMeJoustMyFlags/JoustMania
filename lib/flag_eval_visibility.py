"""Visibility for feature-flag evaluation fallbacks (Issue #921).

The typed getters in :mod:`lib.feature_flags` return a hardcoded default on
*any* failure. Two failure shapes are easy to miss:

1. **Raised exceptions** — provider init blew up, network died, etc. These are
   caught by ``except Exception`` in the getters.
2. **Silent error reasons** — OpenFeature's ``get_*_value`` convenience methods
   never raise; a TYPE_MISMATCH on a numeric flag, a top-level LIST flag read
   over the RPC resolver, or a protobuf gencode mismatch all come back as the
   *default value* with ``reason == Reason.ERROR`` and a populated
   ``error_code``. The old code only inspected the returned value, so these
   surfaced as mysteriously-default behavior ("silent-defaults failure mode").

This module gives both shapes a single visible treatment:

* a WARN log, **rate-limited to once per (flag_key, reason)** so a hot read
  loop cannot spam the log, and
* a ``flag_eval_errors_total{flag_key, reason}`` counter (via
  :mod:`lib.otel_metrics`, per the metrics conventions) so a dashboard/alert
  can see the rate within seconds.

**Startup grace:** during normal startup the provider is briefly not READY, so
``PROVIDER_NOT_READY`` (and the closely related ``PROVIDER_FATAL`` while the
in-process engine is still loading) are *expected* and must not produce WARN
spam or trip an alert. We suppress logging+metrics for those reasons for a
short grace window measured from first import of this module (process start).
After the window, a still-not-ready provider is a real problem and becomes
visible like any other fallback.
"""

from __future__ import annotations

import logging
import os
import time
from threading import Lock

from lib.otel_metrics import Counter

logger = logging.getLogger(__name__)

# flag_eval_errors_total{flag_key, reason}: every fallback to a default that was
# caused by an error (raised exception OR error-reason resolution) bumps this.
# `reason` classifies the failure: an OpenFeature ErrorCode (TYPE_MISMATCH,
# FLAG_NOT_FOUND, PROVIDER_NOT_READY, ...) or an exception class name.
flag_eval_errors_total = Counter(
    "flag_eval_errors_total",
    "Feature-flag evaluations that fell back to their default due to an error",
    labelnames=["flag_key", "reason"],
)

# Grace window (seconds) from process start during which provider-not-ready
# style failures are treated as normal startup races: no WARN, no metric.
# Overridable via env so an operator can widen it on a slow host.
_STARTUP_GRACE_SECONDS = float(os.environ.get("FLAG_EVAL_STARTUP_GRACE_SECONDS", "15"))

# Monotonic timestamp of module import ~= process start.
_PROCESS_START = time.monotonic()

# Error reasons considered "still warming up" and therefore suppressed during
# the startup grace window only.
_STARTUP_GRACE_REASONS = frozenset({"PROVIDER_NOT_READY", "PROVIDER_FATAL"})

# Remembered (flag_key, reason) pairs already warned about, so each distinct
# failure logs at WARN exactly once for the life of the process. The counter
# still increments every time, so rate/alerting is unaffected.
_warned_keys: set[tuple[str, str]] = set()
_warned_lock = Lock()


def _in_startup_grace() -> bool:
    """True while we are still inside the post-start grace window."""
    return (time.monotonic() - _PROCESS_START) < _STARTUP_GRACE_SECONDS


def reset_for_tests() -> None:
    """Reset the once-per-key warn memory. Test-only helper."""
    with _warned_lock:
        _warned_keys.clear()


def record_flag_fallback(flag_key: str, reason: str, detail: str = "") -> None:
    """Record that a flag evaluation fell back to its default due to an error.

    Increments ``flag_eval_errors_total{flag_key, reason}`` and emits a WARN log
    the first time each ``(flag_key, reason)`` pair is seen. Provider-not-ready
    style reasons are suppressed entirely (no log, no metric) while inside the
    startup grace window, so normal startup races stay quiet.

    Args:
        flag_key: The flag whose evaluation fell back to its default.
        reason: A short classification — an OpenFeature ErrorCode value
            (e.g. ``"TYPE_MISMATCH"``) or an exception class name
            (e.g. ``"RuntimeError"``).
        detail: Optional human-readable detail appended to the WARN log only.
    """
    # Swallow startup races: a not-ready provider during the grace window is
    # expected and must not warn or feed the alert.
    if reason in _STARTUP_GRACE_REASONS and _in_startup_grace():
        logger.debug("flag '%s' fallback during startup grace (reason=%s); suppressed", flag_key, reason)
        return

    flag_eval_errors_total.labels(flag_key=flag_key, reason=reason).inc()

    key = (flag_key, reason)
    with _warned_lock:
        already_warned = key in _warned_keys
        if not already_warned:
            _warned_keys.add(key)

    if not already_warned:
        suffix = f": {detail}" if detail else ""
        logger.warning(
            "Feature flag '%s' fell back to its default (reason=%s)%s. "
            "This is logged once per (flag_key, reason); see flag_eval_errors_total for the rate.",
            flag_key,
            reason,
            suffix,
        )
