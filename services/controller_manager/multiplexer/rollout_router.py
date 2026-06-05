"""
RolloutRouter — consumes the ``rollout`` flagd domain to drive progressive
backend rollout across the controller fleet.

The ``rollout`` domain defines:
  * ``strategy``               — "off" | "immediate" | "progressive"
  * ``target_backend``         — the adapter_type to roll toward (e.g. "unstable")
  * ``current_controller_count`` — int cohort size for "progressive" (0/1/3/6/99)

``target_for(serial, all_serials)`` returns the target adapter_type for a serial,
or ``None`` when the rollout does not apply to it:

  * strategy "off"          -> None for every serial
  * strategy "immediate"    -> target_backend for every serial
  * strategy "progressive"  -> target_backend if the serial is in the first
    ``current_controller_count`` serials by sorted order, else None

Flags are evaluated cheaply per routing cycle via the in-process resolver,
mirroring how ``bluetooth_backend`` is evaluated per cycle in the multiplexer —
no caching layer is needed. Any flag/evaluation error degrades to ``None`` so a
flagd outage can never break controller routing.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_DOMAIN = "rollout"


class RolloutRouter:
    """Evaluates the ``rollout`` domain to pick a target backend per serial."""

    def target_for(self, serial: str, all_serials: list[str]) -> str | None:
        """Return the rollout target adapter_type for ``serial``, or None.

        Defensive: any failure to read the rollout flags returns None so that
        routing always falls back to the explicit-targeting / default path.
        """
        try:
            from openfeature.evaluation_context import EvaluationContext

            from lib.feature_flags import get_flag_client

            client = get_flag_client(_DOMAIN)

            strategy = client.get_string_value("strategy", "off", EvaluationContext())
            if strategy == "off":
                return None

            target = client.get_string_value(
                "target_backend",
                "",
                EvaluationContext(targeting_key=serial),
            )
            if not target:
                return None

            if strategy == "immediate":
                return target

            if strategy == "progressive":
                count = client.get_integer_value(
                    "current_controller_count",
                    0,
                    EvaluationContext(),
                )
                if count <= 0:
                    return None
                cohort = sorted(all_serials)[:count]
                return target if serial in cohort else None

            # Unknown strategy -> behave like "off".
            return None
        except Exception:
            logger.debug(
                "RolloutRouter.target_for failed for %s; falling back to None",
                serial,
                exc_info=True,
            )
            return None
