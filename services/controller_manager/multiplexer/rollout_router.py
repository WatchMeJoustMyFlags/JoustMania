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

``current_target()`` (telemetry only) instead raises ``RolloutReadError`` on a
failed read, so the bluetooth-health span can tell "rollout off" from "flags
unreadable" (UC-050 A5, #1489).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_DOMAIN = "rollout"


class RolloutReadError(Exception):
    """The ``rollout`` flags could not be read (flagd error, provider not ready)."""


def _checked(details):
    """Return the evaluated value, raising ``RolloutReadError`` on an error code.

    The OpenFeature SDK never raises from ``get_*_value``: on any provider error
    it returns the caller's default, which for ``strategy`` is "off". The
    ``*_details`` evaluation carries the error code that reveals the failure.
    """
    if details.error_code is not None:
        raise RolloutReadError(f"{details.flag_key}: {details.error_code} {details.error_message or ''}".strip())
    return details.value


class RolloutRouter:
    """Evaluates the ``rollout`` domain to pick a target backend per serial."""

    def current_target(self) -> tuple[str, int]:
        """Return the window-level rollout summary ``(target_backend, count)``.

        Used by the bluetooth-health span to report which backend the rollout is
        steering toward and how many controllers are in the cohort. Returns
        ``("", 0)`` only when the rollout is genuinely off (strategy "off" or no
        target). Raises ``RolloutReadError`` when a flag cannot be read, so the
        span can report the failure instead of a false "off" (#1489); the caller
        must catch it so a flagd outage never breaks span emission.

        For ``immediate`` the cohort is effectively the whole fleet, but the
        ``current_controller_count`` flag is only meaningful for ``progressive``;
        callers treat a 0 count as "not a bounded cohort".
        """
        try:
            from openfeature.evaluation_context import EvaluationContext

            from lib.feature_flags import get_flag_client

            client = get_flag_client(_DOMAIN)

            strategy = _checked(client.get_string_details("strategy", "off", EvaluationContext()))
            if strategy == "off":
                return "", 0

            target = _checked(client.get_string_details("target_backend", "", EvaluationContext()))
            if not target:
                return "", 0

            count = _checked(
                client.get_integer_details(
                    "current_controller_count",
                    0,
                    EvaluationContext(),
                )
            )
            return target, max(0, int(count))
        except RolloutReadError:
            raise
        except Exception as exc:
            raise RolloutReadError(str(exc)) from exc

    def current_strategy(self) -> str:
        """Return the active rollout ``strategy`` string for telemetry.

        One of ``"off"`` | ``"immediate"`` | ``"progressive"`` (or any future
        value flagd returns). Degrades to ``""`` on any evaluation error so a
        flagd outage never breaks span emission; callers treat ``""`` as "no
        strategy observed" and fall back to their default behavior.
        """
        try:
            from openfeature.evaluation_context import EvaluationContext

            from lib.feature_flags import get_flag_client

            client = get_flag_client(_DOMAIN)
            return client.get_string_value("strategy", "off", EvaluationContext())
        except Exception:
            logger.debug("RolloutRouter.current_strategy failed; reporting empty", exc_info=True)
            return ""

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
