"""
MultiplexerBackend — orchestrator that routes per-controller operations
through ControllerIOAdapter instances with centralized state tracking.

LED state, rumble, and effect tracking are centralized here instead of
being duplicated across each backend.

Adapter routing uses OpenFeature targeting via the ``bluetooth_backend``
flag (controller domain) to decide which adapter handles each controller
when multiple adapters
discover the same serial.  Changing the flag value takes effect on the next
discovery cycle — no reconnect needed.
"""

from __future__ import annotations

import logging
import threading
import time

from services.controller_manager import metrics
from services.controller_manager.backend import ControllerBackend
from services.controller_manager.multiplexer.adapter import ControllerIOAdapter
from services.controller_manager.multiplexer.rollout_router import RolloutRouter

logger = logging.getLogger(__name__)


def _adapter_owner(adapter: ControllerIOAdapter) -> ControllerIOAdapter:
    """Return the underlying device-owning adapter.

    Decorators that share a single inner adapter (e.g. UnstableAdapter) expose
    it via ``.inner``. Routing uses owner identity to avoid closing the shared
    device when a serial switches between a plain adapter and a decorator that
    wraps the same inner.
    """
    inner = getattr(adapter, "inner", None)
    return inner if inner is not None else adapter


class MultiplexerBackend(ControllerBackend):
    """Orchestrator that routes per-controller calls through adapters.

    Centralized LED/rumble/effect state tracking.
    Calls adapter.set_output() with combined LED + rumble.
    Adapter assignment tracked in _serial_to_adapter.
    """

    def __init__(
        self,
        adapters: list[ControllerIOAdapter],
        routing_only_adapters: list[ControllerIOAdapter] | None = None,
    ):
        if not adapters:
            raise ValueError("MultiplexerBackend requires at least one adapter")

        self._adapters = adapters

        # Routing-only adapters (e.g. UnstableAdapter) are valid targeting/rollout
        # destinations but do NOT discover independently — they share an inner
        # adapter that is already a discoverer. They are reachable only when
        # explicit targeting or rollout selects their adapter_type.
        self._routing_only_adapters = routing_only_adapters or []

        # Adapter-based routing
        self._serial_to_adapter: dict[str, ControllerIOAdapter] = {}

        # Centralized state
        self._led_colors: dict[str, tuple[int, int, int]] = {}
        self._rumble: dict[str, int] = {}
        self._last_sent_color: dict[str, tuple[int, int, int]] = {}
        self._last_led_update: dict[str, float] = {}
        self._effect_active: set[str] = set()
        self._led_lock = threading.Lock()

        # Build adapter_type -> adapter lookup for targeting resolution.
        # Routing-only adapters are registered too, but added last so they never
        # shadow a discoverer of the same type.
        self._adapter_by_type: dict[str, ControllerIOAdapter] = {}
        for adapter in (*self._routing_only_adapters, *self._adapters):
            self._adapter_by_type[adapter.adapter_type] = adapter

        # Rollout routing (rollout flagd domain). Evaluated per cycle.
        self._rollout_router = RolloutRouter()

        # Discovery throttle: only run full enumeration every N seconds
        self._last_full_discovery: float = 0.0
        self._full_discovery_interval: float = 0.5  # seconds

        # Health counter: LED failures accumulated between stream frames
        self._led_failures: dict[str, int] = {}

        # Track last-known backend per serial for metric cleanup on switch
        self._last_backend: dict[str, str] = {}

        adapter_names = [a.adapter_type for a in self._adapters]
        logger.info(f"MultiplexerBackend created with adapters: {adapter_names}")

    @property
    def adapters(self) -> list[ControllerIOAdapter]:
        """Expose adapters for MockAdapter detection in server.py."""
        return self._adapters

    # -- Fleet-level methods --------------------------------------------------

    async def initialize(self) -> bool:
        any_success = False
        for adapter in self._adapters:
            try:
                serials = adapter.discover()
                for serial in serials:
                    adapter.open(serial)
                    self._serial_to_adapter[serial] = adapter
                any_success = True
                logger.info(f"Initialized {adapter.adapter_type} adapter with {len(serials)} controllers")
            except Exception:
                logger.exception(f"Failed to initialize {adapter.adapter_type} adapter")
        return any_success

    async def shutdown(self):
        for adapter in self._adapters:
            try:
                adapter.close_all()
            except Exception:
                logger.exception(f"Failed to shutdown {adapter.adapter_type} adapter")

        self._serial_to_adapter.clear()
        self._led_colors.clear()
        self._rumble.clear()
        self._last_sent_color.clear()
        self._last_led_update.clear()
        self._effect_active.clear()
        self._last_backend.clear()

    def get_connected_controllers(self, force_rescan: bool = False) -> list[str]:
        seen = self._route_controllers(force_rescan)
        self._serial_to_adapter = seen
        self._cleanup_stale_state(seen)
        self._update_controller_metrics(seen)
        return list(seen.keys())

    def _route_controllers(self, force: bool) -> dict[str, ControllerIOAdapter]:
        """All adapters discover independently, routing deduplicates.

        Phase 1 — Discover: every adapter enumerates + opens what it finds.
        Phase 2 — Route: for serials seen by multiple adapters, pick one via
                  OpenFeature targeting. Close handles on non-winning adapters.
        """
        now = time.monotonic()
        if force or (now - self._last_full_discovery >= self._full_discovery_interval):
            verify_only = False
            self._last_full_discovery = now
        else:
            verify_only = True

        if verify_only:
            metrics.discovery_verify_only_total.inc()
        else:
            metrics.discovery_full_enumerate_total.inc()

        # Phase 1: all adapters discover independently.
        # Each adapter receives exclude_serials — serials claimed by other adapters
        # in the previous cycle. Adapters that manage HID handles (e.g. rust) skip
        # opening excluded devices, avoiding open→close churn that causes LED flashing.
        candidates: dict[str, list[ControllerIOAdapter]] = {}
        for adapter in self._adapters:
            claimed_by_others = [s for s, a in self._serial_to_adapter.items() if a is not adapter]
            for serial in adapter.discover(force=force, verify_only=verify_only, exclude_serials=claimed_by_others):
                candidates.setdefault(serial, []).append(adapter)

        # Phase 2: route — pick winner for each serial
        candidate_serials = list(candidates.keys())
        seen: dict[str, ControllerIOAdapter] = {}
        for serial, adapters in candidates.items():
            # Mock-only → skip targeting
            if len(adapters) == 1 and adapters[0].adapter_type == "mock":
                seen[serial] = adapters[0]
                metrics.controller_routing_decisions_total.labels(
                    serial=serial,
                    adapter="mock",
                    method="default",
                ).inc()
                continue

            preferred, preferred_method = self._resolve_adapter_for_serial(serial, candidate_serials)
            winner = None
            method = "default"

            if preferred and preferred in adapters:
                winner = preferred
                method = preferred_method
            elif preferred:
                # Preferred adapter didn't discover it — try open on demand.
                # Routing-only adapters (e.g. unstable) share an inner that IS a
                # discoverer, so their open() reaches the already-open device.
                if preferred.open(serial):
                    winner = preferred
                    method = preferred_method
                else:
                    method = "fallback"

            if winner is None:
                # Pick first non-mock (excluding routing-only types like
                # "unstable", which must never be a default/fallback winner),
                # or first overall.
                winner = next(
                    (a for a in adapters if a.adapter_type not in ("mock", "unstable")),
                    adapters[0],
                )

            seen[serial] = winner
            winner_owner = _adapter_owner(winner)

            # Close handles on non-winning adapters. Skip any loser that shares
            # the winner's underlying device owner (shared-inner case), else we
            # would tear down the device the winner is using.
            for adapter in adapters:
                if adapter is not winner and _adapter_owner(adapter) is not winner_owner:
                    adapter.close(serial)

            metrics.controller_routing_decisions_total.labels(
                serial=serial,
                adapter=winner.adapter_type,
                method=method,
            ).inc()

            # Detect dynamic switch from previous cycle
            old = self._serial_to_adapter.get(serial)
            if old and old is not winner and old not in adapters and _adapter_owner(old) is not winner_owner:
                old.close(serial)
                logger.info(f"Switched {serial}: {old.adapter_type} -> {winner.adapter_type}")

        return seen

    def _resolve_adapter_for_serial(
        self,
        serial: str,
        candidate_serials: list[str] | None = None,
    ) -> tuple[ControllerIOAdapter | None, str]:
        """Resolve the preferred adapter for a serial via three-way precedence.

        Precedence (highest to lowest):
          1. Explicit per-serial ``bluetooth_backend`` targeting — honored only
             when the evaluation reason is ``TARGETING_MATCH`` (an actual rule
             match, not the flag's default variant).
          2. Rollout-driven — :meth:`RolloutRouter.target_for` over the full
             candidate serial set.
          3. Default — the flag's resolved value (default variant / fallback).

        Returns ``(adapter, method)`` where ``method`` is one of
        ``"targeted"`` / ``"rollout"`` / ``"default"``. ``adapter`` is ``None``
        when nothing resolves (targeting unavailable, error, or no matching
        adapter), in which case ``method`` is ``"default"``.
        """
        try:
            from openfeature.evaluation_context import EvaluationContext
            from openfeature.flag_evaluation import Reason

            from lib.feature_flags import get_flag_client

            client = get_flag_client("controller")

            # 1. Explicit per-serial targeting wins outright.
            details = client.get_string_details(
                "bluetooth_backend",
                "",
                EvaluationContext(targeting_key=serial),
            )
            if details.value and str(details.reason) == Reason.TARGETING_MATCH.value:
                adapter = self._adapter_by_type.get(details.value)
                if adapter is not None:
                    return adapter, "targeted"

            # 2. Rollout-driven selection.
            rollout_target = self._rollout_router.target_for(
                serial,
                candidate_serials or [serial],
            )
            if rollout_target:
                adapter = self._adapter_by_type.get(rollout_target)
                if adapter is not None:
                    return adapter, "rollout"

            # 3. Default: the flag's resolved value (e.g. default variant).
            if details.value:
                adapter = self._adapter_by_type.get(details.value)
                if adapter is not None:
                    return adapter, "default"
        except Exception:
            logger.debug(f"Failed to resolve adapter for {serial}", exc_info=True)
        return None, "default"

    def get_adapter_type(self, serial: str) -> str:
        """Return the adapter_type string for a tracked serial."""
        adapter = self._serial_to_adapter.get(serial)
        return adapter.adapter_type if adapter else "unknown"

    def get_controller_metadata(self, serial: str) -> dict | None:
        """Return adapter-supplied metadata (e.g. reserved/tag) for a serial.

        Delegates to the owning adapter's get_metadata() if it provides one
        (the MockAdapter does). Returns None when unavailable.
        """
        adapter = self._serial_to_adapter.get(serial)
        if adapter is not None and hasattr(adapter, "get_metadata"):
            return adapter.get_metadata(serial)
        return None

    def rollout_summary(self) -> tuple[str, int]:
        """Window-level rollout summary ``(target_backend, cohort_count)``.

        Public seam for telemetry (the bluetooth-health span); delegates to the
        RolloutRouter, which degrades to ``("", 0)`` on any flag error.
        """
        return self._rollout_router.current_target()

    def _cleanup_stale_state(self, seen: dict[str, ControllerIOAdapter]) -> None:
        """Remove centralized state for controllers no longer present."""
        stale = set(self._led_colors.keys()) - set(seen.keys())
        for serial in stale:
            self._led_colors.pop(serial, None)
            self._rumble.pop(serial, None)
            self._last_sent_color.pop(serial, None)
            self._last_led_update.pop(serial, None)
            self._effect_active.discard(serial)
            # Clear backend info metric so disconnected controllers don't
            # appear in dashboard counts
            prev = self._last_backend.pop(serial, None)
            if prev:
                metrics.controller_backend_info.labels(serial=serial, backend=prev).set(0)

    def _update_controller_metrics(self, seen: dict[str, ControllerIOAdapter]) -> None:
        """Update Prometheus metrics for connected controllers."""
        for serial, adapter in seen.items():
            backend = adapter.adapter_type
            # Clear old backend label when backend changes (e.g. rust -> python)
            prev = self._last_backend.get(serial)
            if prev and prev != backend:
                metrics.controller_backend_info.labels(serial=serial, backend=prev).set(0)
            self._last_backend[serial] = backend
            metrics.controller_backend_info.labels(serial=serial, backend=backend).set(1)
            hci = adapter.get_adapter_for_serial(serial)
            if hci:
                metrics.controller_adapter_info.labels(serial=serial, adapter=hci).set(1)

    def update_all_leds(self) -> int:
        """Centralized LED refresh with keep-alive and color-change detection."""
        current_time = time.time()
        updated_count = 0

        # list() is intentional — dict may be modified by concurrent disconnect
        for serial, stored_color in list(self._led_colors.items()):  # NOSONAR(S7504)
            if serial in self._effect_active:
                continue

            adapter = self._serial_to_adapter.get(serial)
            if not adapter:
                continue

            try:
                last_sent = self._last_sent_color.get(serial)
                last_update = self._last_led_update.get(serial, 0)

                color_changed = stored_color != last_sent
                keepalive_needed = current_time - last_update >= 2.0

                if color_changed or keepalive_needed:
                    r, g, b = stored_color
                    rumble = self._rumble.get(serial, 0)
                    with self._led_lock:
                        result = adapter.set_output(serial, r, g, b, rumble)
                    if not result:
                        self._led_failures[serial] = self._led_failures.get(serial, 0) + 1
                    self._last_sent_color[serial] = stored_color
                    self._last_led_update[serial] = current_time
                    updated_count += 1

                    if color_changed:
                        logger.debug(f"LED color changed for {serial}: {last_sent} -> {stored_color}")

            except Exception as e:
                logger.debug(f"Error updating LED for {serial}: {e}")
                self._led_failures[serial] = self._led_failures.get(serial, 0) + 1

        return updated_count

    async def scan_controllers(self) -> list[dict]:
        results: list[dict] = []
        for adapter in self._adapters:
            for serial in adapter.discover(force=True):
                results.append({"address": serial, "serial": serial, "name": f"Controller {serial[-4:]}"})
        return results

    # -- Per-controller methods -----------------------------------------------

    async def get_controller_state(self, serial: str) -> dict | None:
        adapter = self._serial_to_adapter.get(serial)
        if adapter is None:
            return None
        return adapter.poll(serial)

    def consume_death_frame(self, serial: str) -> None:
        """Route a delivered-frame death-latch tick to the owning adapter (#926).

        Only the MockAdapter implements ``consume_death_frame``; real hardware
        adapters do not (and do not need) it, so this is a no-op for them. The
        gameplay send path calls this once per streamed controller to drain the
        frame-budget death latch — see mock_adapter.consume_death_frame.
        """
        adapter = self._serial_to_adapter.get(serial)
        consume = getattr(adapter, "consume_death_frame", None)
        if consume is not None:
            consume(serial)

    async def set_led_color(self, serial: str, r: int, g: int, b: int) -> bool:
        adapter = self._serial_to_adapter.get(serial)
        if adapter is None:
            return False
        self._led_colors[serial] = (r, g, b)
        rumble = self._rumble.get(serial, 0)
        # Non-blocking lock: if update_all_leds() batch is in progress, skip
        # the direct USB write to avoid concurrent set_output() calls.
        # _led_colors is already set, so the 20Hz loop picks it up within 50ms.
        if self._led_lock.acquire(blocking=False):
            try:
                result = adapter.set_output(serial, r, g, b, rumble)
                if result:
                    self._last_sent_color[serial] = (r, g, b)
                    self._last_led_update[serial] = time.time()
                return result
            finally:
                self._led_lock.release()
        return True

    async def set_rumble(self, serial: str, intensity: int) -> bool:
        adapter = self._serial_to_adapter.get(serial)
        if adapter is None:
            return False
        self._rumble[serial] = intensity
        r, g, b = self._led_colors.get(serial, (0, 0, 0))
        # Non-blocking lock: same pattern as set_led_color() — skip direct
        # USB write if batch is in progress. Rumble + color state is stored,
        # next update_all_leds() cycle sends the combined output.
        if self._led_lock.acquire(blocking=False):
            try:
                return adapter.set_output(serial, r, g, b, intensity)
            finally:
                self._led_lock.release()
        return True

    def set_effect_active(self, serial: str, active: bool):
        if active:
            self._effect_active.add(serial)
        else:
            self._effect_active.discard(serial)

    async def connect_controller(self, address: str) -> bool:
        # Try targeted adapter first
        preferred, _method = self._resolve_adapter_for_serial(address, [address])
        if preferred:
            try:
                if preferred.open(address):
                    self._serial_to_adapter[address] = preferred
                    return True
            except Exception:
                logger.exception(f"connect_controller targeted failed for {preferred.adapter_type}")

        # Fall back to iteration order
        for adapter in self._adapters:
            if adapter is preferred:
                continue  # Already tried
            try:
                if adapter.open(address):
                    self._serial_to_adapter[address] = adapter
                    return True
            except Exception:
                logger.exception(f"connect_controller failed for {adapter.adapter_type}")
        return False

    async def disconnect_controller(self, serial: str) -> bool:
        adapter = self._serial_to_adapter.get(serial)
        if adapter is None:
            return False
        adapter.close(serial)
        self._serial_to_adapter.pop(serial, None)
        self._led_colors.pop(serial, None)
        self._rumble.pop(serial, None)
        self._last_sent_color.pop(serial, None)
        self._last_led_update.pop(serial, None)
        self._effect_active.discard(serial)
        prev = self._last_backend.pop(serial, None)
        if prev:
            metrics.controller_backend_info.labels(serial=serial, backend=prev).set(0)
        return True
