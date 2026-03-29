"""
FlagdSampler — Dynamic trace sampling via OpenFeature/flagd.

Reads the ``trace_sampling_rate`` flag (float 0.0–1.0) from the flagd
``performance`` domain. Flag values are cached for 2 seconds to avoid
per-decision flagd calls at high sampling rates (up to 1 000 Hz).
"""

import logging
import time

from opentelemetry.context import Context
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
from opentelemetry.trace import Link, SpanKind
from opentelemetry.trace.span import TraceState
from opentelemetry.util.types import Attributes

logger = logging.getLogger(__name__)

_CACHE_TTL = 2.0  # seconds


class FlagdSampler(Sampler):
    """Sampler that reads trace_sampling_rate from flagd."""

    def __init__(self) -> None:
        self._cached_rate: float = 1.0
        self._last_fetch: float = 0.0

    def should_sample(  # noqa: ARG002
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes | None = None,
        links: list[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        rate = self._get_rate(attributes)
        # Use trace_id for deterministic sampling (consistent across parent/child spans)
        hash_value = (trace_id & 0xFFFFFFFFFFFFFFFF) / (1 << 64)
        if rate >= 1.0 or hash_value < rate:
            return SamplingResult(Decision.RECORD_AND_SAMPLE)
        return SamplingResult(Decision.DROP)

    def get_description(self) -> str:
        return "FlagdSampler"

    # ------------------------------------------------------------------

    def _get_rate(self, attributes: Attributes | None) -> float:
        now = time.monotonic()
        if now - self._last_fetch < _CACHE_TTL:
            return self._cached_rate

        try:
            from lib.feature_flags import get_flag_client

            client = get_flag_client("performance")

            # Build per-evaluation context from span attributes
            eval_ctx = None
            if attributes:
                serial = dict(attributes).get("controller.serial")
                if serial:
                    from openfeature.evaluation_context import EvaluationContext

                    eval_ctx = EvaluationContext(attributes={"controller_serial": str(serial)})

            self._cached_rate = client.get_float_value("trace_sampling_rate", 1.0, eval_ctx)
            self._last_fetch = now
        except Exception:
            # Advance _last_fetch on failure to honor TTL as backoff,
            # preventing per-decision retries when flagd is down.
            self._last_fetch = now
            logger.debug("Failed to read trace_sampling_rate from flagd, using cached value", exc_info=True)
        return self._cached_rate
