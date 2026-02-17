"""
ChaosAdapter — decorator that wraps a ControllerIOAdapter to inject faults.

Uses OpenFeature fractional targeting with controller serial as
targetingKey for consistent (sticky) fault assignment.
Flag changes are picked up reactively via ProviderEvent listener.

Fault types:
  - none: passthrough (no fault)
  - poll_drop: 30% chance poll() returns None
  - accel_spike: 5% chance of injected high acceleration
  - led_failure: 50% chance set_output() returns False
  - disconnect: poll returns None, discover excludes serial, set_output returns False
"""

from __future__ import annotations

import logging
import random
from typing import Any

from lib.controller_constants import AxisKey, StateKey
from services.controller_manager import metrics
from services.controller_manager.multiplexer.adapter import ControllerIOAdapter

logger = logging.getLogger(__name__)


class ChaosAdapter(ControllerIOAdapter):
    """Decorator that wraps an adapter to inject configurable faults.

    Uses OpenFeature fractional targeting with controller serial as
    targetingKey for consistent (sticky) fault assignment.
    Flag changes are picked up reactively via ProviderEvent listener.
    """

    def __init__(self, inner: ControllerIOAdapter):
        self._inner = inner
        self._fault_map: dict[str, str] = {}  # serial -> fault type
        self._known_serials: set[str] = set()
        self._listener_registered = False

    @property
    def adapter_type(self) -> str:
        return self._inner.adapter_type  # transparent to routing

    def discover(self, force: bool = False) -> list[str]:
        serials = self._inner.discover(force=force)
        # Track and evaluate new serials
        for s in serials:
            if s not in self._known_serials:
                self._known_serials.add(s)
                self._fault_map[s] = self._evaluate_flag(s)
        self._ensure_listener()
        return [s for s in serials if self._fault_map.get(s) != "disconnect"]

    def open(self, serial: str) -> bool:
        return self._inner.open(serial)

    def poll(self, serial: str) -> dict | None:
        fault = self._get_fault(serial)
        if fault == "disconnect":
            return None
        if fault == "poll_drop" and random.random() < 0.3:
            metrics.chaos_faults_injected_total.labels(fault="poll_drop").inc()
            return None
        result = self._inner.poll(serial)
        if fault == "accel_spike" and result and random.random() < 0.05:
            result[StateKey.ACCEL] = {
                AxisKey.X: random.gauss(0, 3.0),
                AxisKey.Y: random.gauss(0, 3.0),
                AxisKey.Z: random.gauss(0, 3.0),
            }
            metrics.chaos_faults_injected_total.labels(fault="accel_spike").inc()
        return result

    def set_output(self, serial: str, r: int, g: int, b: int, rumble: int) -> bool:
        fault = self._get_fault(serial)
        if fault == "disconnect":
            metrics.chaos_faults_injected_total.labels(fault="disconnect").inc()
            return False
        if fault == "led_failure" and random.random() < 0.5:
            metrics.chaos_faults_injected_total.labels(fault="led_failure").inc()
            return False
        return self._inner.set_output(serial, r, g, b, rumble)

    def close(self, serial: str) -> None:
        self._inner.close(serial)
        self._known_serials.discard(serial)
        self._fault_map.pop(serial, None)

    def close_all(self) -> None:
        self._inner.close_all()
        self._known_serials.clear()
        self._fault_map.clear()

    def _get_fault(self, serial: str) -> str:
        """Pure dict lookup — zero overhead at 100Hz."""
        fault = self._fault_map.get(serial)
        if fault is None:
            fault = self._evaluate_flag(serial)
            self._fault_map[serial] = fault
            self._known_serials.add(serial)
        return fault

    def _evaluate_flag(self, serial: str) -> str:
        try:
            from openfeature.evaluation_context import EvaluationContext

            from lib.feature_flags import get_flag_client

            client = get_flag_client("performance")
            return client.get_string_value(
                "chaos_fault_type",
                "none",
                EvaluationContext(targeting_key=serial),
            )
        except Exception:
            return "none"

    def _ensure_listener(self) -> None:
        """Register flag change listener once."""
        if self._listener_registered:
            return
        try:
            from openfeature.provider import ProviderEvent

            from lib.feature_flags import get_flag_client

            client = get_flag_client("performance")
            client.add_handler(
                ProviderEvent.PROVIDER_CONFIGURATION_CHANGED,
                self._on_flag_changed,
            )
            self._listener_registered = True
        except Exception:
            pass

    def _on_flag_changed(self, _event: Any) -> None:
        """Re-evaluate all known serials when flag config changes."""
        for serial in self._known_serials:
            self._fault_map[serial] = self._evaluate_flag(serial)

    def __getattr__(self, name: str) -> Any:
        """Delegate unknown attributes to inner adapter (e.g., MockAdapter extras)."""
        return getattr(self._inner, name)
