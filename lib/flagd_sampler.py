"""
FlagdSampler — Dynamic trace sampling via OpenFeature/flagd.

Reads the ``trace_sampling_rate`` flag (float 0.0–1.0) from the flagd
``performance`` domain on every sampling decision. Because the flagd
in-process provider syncs flag state to local memory, each evaluation
is a cheap local read — safe even at 1 000 Hz.
"""

import logging

from opentelemetry.context import Context
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
from opentelemetry.trace import Link, SpanKind
from opentelemetry.util.types import Attributes

logger = logging.getLogger(__name__)


class FlagdSampler(Sampler):
    """Sampler that reads trace_sampling_rate from flagd."""

    def should_sample(  # noqa: ARG002
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes | None = None,
        links: list[Link] | None = None,
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

    @staticmethod
    def _get_rate(attributes: Attributes | None) -> float:
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

            return client.get_float_value("trace_sampling_rate", 1.0, eval_ctx)
        except Exception:
            logger.debug("Failed to read trace_sampling_rate from flagd, defaulting to 1.0", exc_info=True)
            return 1.0
