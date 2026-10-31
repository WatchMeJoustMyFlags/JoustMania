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

import asyncio
import logging
import threading
import time

from services.controller_manager import metrics
from services.controller_manager.backend import ControllerBackend
from services.controller_manager.multiplexer.adapter import ControllerIOAdapter, DiscoveryFailedError
from services.controller_manager.multiplexer.rollout_router import RolloutRouter

logger = logging.getLogger(__name__)

# The LED batch resends an unchanged output at least this often, so the
# controller's LED (which times out on the hardware) stays lit.
LED_KEEPALIVE_S = 2.0
# Bound on one adapter's startup Discover + Open pass (#2351). The adapters start
# concurrently, so this is also the bound on the whole startup: under the server's
# 10 s wait, a wedged HID service leaves its adapter with no controllers (the
# discovery loop finds them later) instead of holding startup.
INIT_ADAPTER_TIMEOUT_S = 8.0

# Consecutive failed discovery passes of one adapter before its controllers are
# dropped (UC-002 A1, #2125). Until then the adapter keeps its previous routes,
# so one slow or failed Discover mid-game disconnects nobody.
MAX_FAILED_DISCOVERY_PASSES = 3


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

        # Adapter-based routing. Copy-on-write: the table is never mutated in
        # place, every change publishes a new dict, so the lock-free readers
        # (100 Hz state loop, LED batch) and the discovery thread iterating it
        # always see a consistent snapshot (#1478).
        self._serial_to_adapter: dict[str, ControllerIOAdapter] = {}
        # Guards writes to _serial_to_adapter and _closed. Never held across
        # adapter I/O: _set_route holds it for a dict copy, and the discovery
        # swap holds it for the swap plus _cleanup_stale_state (in-memory pops
        # and metric series removal, CPU only).
        self._routing_lock = threading.Lock()
        # Set by shutdown(). A discovery pass still running in its worker
        # thread then installs no routes and closes what it opened (#1630).
        self._closed = False

        # Centralized state
        self._led_colors: dict[str, tuple[int, int, int]] = {}
        self._rumble: dict[str, int] = {}
        # Serials whose desired output was written while they had no route (a
        # disconnect): the stale-state cleanup keeps that output, and the LED batch
        # skips the serial, and a write to it is held too, until it registers again
        # (``force_led_resend``, called before the owner's colour), so a kept output
        # never goes out ahead of the owner's (UC-002 BR-008, #2471).
        # Written under _routing_lock, like the cleanup that reads it.
        self._unrouted_output: set[str] = set()
        # Last output (colour + rumble) a write accepted, and when (#2355).
        # Only a successful write updates them, so the batch resends a
        # changed or failed output on its next tick.
        self._last_sent_color: dict[str, tuple[int, int, int]] = {}
        self._last_sent_rumble: dict[str, int] = {}
        self._last_led_update: dict[str, float] = {}
        # Time of the last failure counted in _led_failures: a failing
        # controller is retried every tick but counted once per keep-alive
        # interval, whatever its output does meanwhile.
        self._led_failures_counted: dict[str, float] = {}
        self._effect_active: set[str] = set()
        # Serials whose output the next batch sends even when unchanged: a controller
        # that (re)registered, whose first write after the re-open may have been
        # dropped (UC-002 BR-008, #2466). Only a batch write that the adapter accepted
        # clears it; a direct write does not, so the batch always repeats it once.
        # set.add / discard from the event loop need no lock.
        self._force_led_resend: set[str] = set()
        self._led_lock = threading.Lock()

        # Build adapter_type -> adapter lookup for targeting resolution.
        # Routing-only adapters are registered too, but added last so they never
        # shadow a discoverer of the same type.
        self._adapter_by_type: dict[str, ControllerIOAdapter] = {}
        for adapter in (*self._routing_only_adapters, *self._adapters):
            self._adapter_by_type[adapter.adapter_type] = adapter

        # Rollout routing (rollout flagd domain). Evaluated per cycle.
        self._rollout_router = RolloutRouter()

        # Consecutive failed discovery passes per adapter (UC-002 A1, #2125).
        # Only the discovery worker thread reads and writes it.
        self._failed_discovery_passes: dict[ControllerIOAdapter, int] = {}

        # Health counter: LED failures accumulated between stream frames
        self._led_failures: dict[str, int] = {}

        # Track last-known backend per serial for metric cleanup on switch
        self._last_backend: dict[str, str] = {}
        # Last HCI adapter exported in controller_adapter_info per serial, so
        # the old series can be removed when the controller moves or goes (#1468).
        self._last_adapter: dict[str, str] = {}
        # Guards _last_backend / _last_adapter and their info series. The
        # discovery worker thread exports them and removes stale ones while
        # shutdown() (event loop) clears them; a worker set(1) landing after the
        # clear would otherwise leave an orphaned series (#1468). Never held
        # across I/O, only on the discovery / shutdown paths, not per poll.
        self._info_series_lock = threading.Lock()

        adapter_names = [a.adapter_type for a in self._adapters]
        logger.info(f"MultiplexerBackend created with adapters: {adapter_names}")

    @property
    def adapters(self) -> list[ControllerIOAdapter]:
        """Expose adapters for MockAdapter detection in server.py."""
        return self._adapters

    # -- Fleet-level methods --------------------------------------------------

    async def initialize(self) -> bool:
        """Discover and open each adapter's controllers, off the event loop (#2351).

        Discover / Open are blocking gRPC calls (5 s deadline each against a wedged
        rust-hid): they run in a worker thread, bounded by ``INIT_ADAPTER_TIMEOUT_S``,
        so the event loop stays free and the server's startup timeout can fire. Routes
        are set here, on the loop, only for a pass that finished in time; an adapter
        that timed out starts with no controllers and the discovery loop retries.
        """
        with self._routing_lock:
            self._closed = False
        results = await asyncio.gather(*(self._initialize_adapter(adapter) for adapter in self._adapters))
        return any(results)

    async def _initialize_adapter(self, adapter: ControllerIOAdapter) -> bool:
        """One adapter's bounded startup pass; True when the adapter counts as started."""
        try:
            serials = await asyncio.wait_for(
                asyncio.to_thread(self._discover_and_open, adapter), timeout=INIT_ADAPTER_TIMEOUT_S
            )
        except TimeoutError:
            # Like a failed Discover: the backend is not answering yet, discovery retries.
            logger.warning(
                f"{adapter.adapter_type} startup discovery took over {INIT_ADAPTER_TIMEOUT_S}s; "
                "starting with no controllers"
            )
            return True
        except Exception:
            logger.exception(f"Failed to initialize {adapter.adapter_type} adapter")
            return False
        for serial in serials:
            self._set_route(serial, adapter)
        logger.info(f"Initialized {adapter.adapter_type} adapter with {len(serials)} controllers")
        return True

    @staticmethod
    def _discover_and_open(adapter: ControllerIOAdapter) -> list[str]:
        """Worker thread: one adapter's startup Discover and Open calls; returns the serials to route."""
        try:
            serials = adapter.discover()
        except DiscoveryFailedError as e:
            # Backend not up yet: start with no controllers, discovery retries.
            logger.warning(f"{adapter.adapter_type} discovery failed at startup: {e}")
            return []
        for serial in serials:
            adapter.open(serial)
        return serials

    async def shutdown(self):
        # Mark closed BEFORE closing the adapters: a discovery pass that reopens
        # a device after close_all() then sees the flag at its swap and closes
        # that handle again instead of installing a route to it (#1630).
        with self._routing_lock:
            self._closed = True
        for adapter in self._adapters:
            try:
                adapter.close_all()
            except Exception:
                logger.exception(f"Failed to shutdown {adapter.adapter_type} adapter")

        # Same lock order as the discovery swap: _led_lock, then _routing_lock.
        with self._led_lock, self._routing_lock:
            self._serial_to_adapter = {}
            self._led_colors.clear()
            self._unrouted_output.clear()
            self._rumble.clear()
            self._last_sent_color.clear()
            self._last_sent_rumble.clear()
            self._last_led_update.clear()
            self._led_failures_counted.clear()
            self._led_failures.clear()
            self._force_led_resend.clear()
            self._effect_active.clear()
        # Remove the controller_*_info series too, so a stopped service does not
        # leave its controllers counted (#1726). Under the lock: a worker set(1)
        # is skipped by _update_controller_metrics once the table is empty.
        with self._info_series_lock:
            for serial, backend_name in self._last_backend.items():
                metrics.controller_backend_info.remove(serial, backend_name)
            for serial, hci in self._last_adapter.items():
                metrics.controller_adapter_info.remove(serial, hci)
            self._last_backend.clear()
            self._last_adapter.clear()

    def _set_route(self, serial: str, adapter: ControllerIOAdapter) -> None:
        """Assign one serial's adapter, copy-on-write (used by initialize)."""
        with self._routing_lock:
            self._serial_to_adapter = {**self._serial_to_adapter, serial: adapter}

    def get_connected_controllers(self, force_rescan: bool = False) -> list[str]:
        if self._closed:
            return []  # discover() would reopen the devices shutdown() closed
        discovered = self._route_controllers(force_rescan)
        # Discovery and the 20 Hz LED batch run in separate worker threads
        # (#1447). The routing swap and the stale-state cleanup happen under
        # the LED lock, and update_all_leds re-reads the colour and the adapter
        # under the same lock, so a batch never writes to a serial this cleanup
        # removed and never re-creates its LED bookkeeping. Loser / switched
        # adapters are closed earlier, in _route_controllers, outside the lock:
        # a batch that runs between that close and this swap may still write
        # to the closed handle once; set_output then fails and the failure is
        # counted in _led_failures.
        #
        # Lock order: _led_lock, then _routing_lock (shutdown uses the same).
        with self._led_lock, self._routing_lock:
            closed = self._closed
            if not closed:
                self._serial_to_adapter = discovered
                self._cleanup_stale_state(discovered)
        if closed:
            # shutdown() ran during this pass: install nothing, and close any
            # handle the pass (re)opened after close_all() (#1630).
            for serial, adapter in discovered.items():
                try:
                    adapter.close(serial)
                except Exception:
                    logger.exception(f"Failed to close {serial} after shutdown")
            return []
        self._update_controller_metrics(discovered)
        return list(discovered.keys())

    def _route_controllers(self, force: bool) -> dict[str, ControllerIOAdapter]:
        """All adapters discover independently, routing deduplicates.

        Phase 1 — Discover: every adapter enumerates + opens what it finds.
        Phase 2 — Route: for serials seen by multiple adapters, pick one via
                  OpenFeature targeting. Close handles on non-winning adapters.
        """
        # Every pass is a full enumeration (the discovery check runs at 1 Hz,
        # NFR-002), which UC-002 step 3 needs to find a new controller on the next
        # check. It is cheap: rust-hid enumerates but opens only controllers that
        # are not open yet and never reads a polled one (UC-002 BR-006, #2314).
        metrics.discovery_full_enumerate_total.inc()

        # Phase 1: all adapters discover independently.
        # Each adapter receives exclude_serials — serials claimed by other adapters
        # in the previous cycle. Adapters that manage HID handles (e.g. rust) skip
        # opening excluded devices, avoiding open→close churn that causes LED flashing.
        # One snapshot of the (copy-on-write) table for the whole pass.
        previous = self._serial_to_adapter
        candidates: dict[str, list[ControllerIOAdapter]] = {}
        # Routes kept from the previous pass for an adapter whose discovery failed.
        carried: dict[str, ControllerIOAdapter] = {}
        for adapter in self._adapters:
            claimed_by_others = [s for s, a in previous.items() if a is not adapter]
            try:
                serials = adapter.discover(force=force, exclude_serials=claimed_by_others)
            except DiscoveryFailedError as e:
                carried.update(self._carry_routes_after_failed_discovery(adapter, previous, e))
                continue
            self._failed_discovery_passes.pop(adapter, None)
            for serial in serials:
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

            # UC-002 BR-001: a routing change moves the serial to its new adapter in
            # this cycle; it stays in the returned list, so no disconnect/connect.
            # Detect dynamic switch from previous cycle
            old = previous.get(serial)
            if old and old is not winner and old not in adapters and _adapter_owner(old) is not winner_owner:
                old.close(serial)
                logger.info(f"Switched {serial}: {old.adapter_type} -> {winner.adapter_type}")

        # A serial another adapter found this pass is routed above; the rest of a
        # failed adapter's previous routes stay as they were.
        for serial, adapter in carried.items():
            seen.setdefault(serial, adapter)

        return seen

    def _carry_routes_after_failed_discovery(
        self,
        adapter: ControllerIOAdapter,
        previous: dict[str, ControllerIOAdapter],
        error: DiscoveryFailedError,
    ) -> dict[str, ControllerIOAdapter]:
        """Previous routes owned by ``adapter`` to keep after its failed pass.

        A failed pass is not "no controllers" (UC-002 A1, #2125): the routes stay
        until MAX_FAILED_DISCOVERY_PASSES consecutive passes have failed, then
        they are dropped and the discovery loop reports the controllers lost.
        """
        failures = self._failed_discovery_passes.get(adapter, 0) + 1
        self._failed_discovery_passes[adapter] = failures
        owner = _adapter_owner(adapter)
        owned = {s: a for s, a in previous.items() if _adapter_owner(a) is owner}
        if failures >= MAX_FAILED_DISCOVERY_PASSES:
            logger.warning(
                f"{adapter.adapter_type} discovery failed {failures} passes in a row ({error}): "
                f"dropping {len(owned)} controllers"
            )
            return {}
        logger.warning(
            f"{adapter.adapter_type} discovery failed ({error}), pass {failures}/{MAX_FAILED_DISCOVERY_PASSES}: "
            f"keeping {len(owned)} controllers"
        )
        return owned

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

    def take_led_failures(self, serial: str) -> int:
        """Return and clear the failed LED writes counted for a serial (lock-free, as the dict pop was)."""
        return self._led_failures.pop(serial, 0)

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
        RolloutRouter. ``("", 0)`` means the rollout is off; a failed flag read
        raises ``RolloutReadError`` (#1489).
        """
        return self._rollout_router.current_target()

    def rollout_strategy(self) -> str:
        """Active rollout ``strategy`` string for telemetry.

        Public seam for the bluetooth-health span so the agent can see whether
        the controller-manager is routing ``immediate`` / ``progressive`` /
        ``off``. Delegates to the RolloutRouter, which degrades to ``""`` on any
        flag error.
        """
        return self._rollout_router.current_strategy()

    def _cleanup_stale_state(self, seen: dict[str, ControllerIOAdapter]) -> None:
        """Remove centralized state for controllers no longer present."""
        # Union of every per-serial table: a controller that never received an
        # LED colour still has backend / adapter info series to remove (#1468),
        # and a rumble-only serial is replayed by the LED batch (#2365).
        tracked = set(self._led_colors) | set(self._rumble) | set(self._last_backend) | set(self._last_adapter)
        stale = tracked - set(seen)
        for serial in stale:
            if serial not in self._unrouted_output:
                self._led_colors.pop(serial, None)
                self._rumble.pop(serial, None)
            self._last_sent_color.pop(serial, None)
            self._last_sent_rumble.pop(serial, None)
            self._last_led_update.pop(serial, None)
            self._led_failures.pop(serial, None)
            self._led_failures_counted.pop(serial, None)
            self._effect_active.discard(serial)
            self._force_led_resend.discard(serial)
            # Remove the info series so disconnected controllers don't
            # appear in dashboard counts
            self._remove_info_series(serial)

    def _update_controller_metrics(self, seen: dict[str, ControllerIOAdapter]) -> None:
        """Update Prometheus metrics for connected controllers."""
        with self._info_series_lock:
            for serial, adapter in seen.items():
                # shutdown() may have cleared the table since this pass
                # swapped it in; re-exporting the serial would orphan it.
                if serial not in self._serial_to_adapter:
                    continue
                self._export_info_series(serial, adapter)

    def _export_info_series(self, serial: str, adapter: ControllerIOAdapter) -> None:
        """Export one controller's backend / adapter info series (caller holds _info_series_lock)."""
        backend = adapter.adapter_type
        # Clear old backend label when backend changes (e.g. rust -> python)
        prev = self._last_backend.get(serial)
        if prev and prev != backend:
            metrics.controller_backend_info.remove(serial, prev)
        self._last_backend[serial] = backend
        metrics.controller_backend_info.labels(serial=serial, backend=backend).set(1)
        hci = adapter.get_adapter_for_serial(serial)
        prev_hci = self._last_adapter.get(serial)
        if prev_hci and prev_hci != hci:
            metrics.controller_adapter_info.remove(serial, prev_hci)
            self._last_adapter.pop(serial, None)
        if hci:
            self._last_adapter[serial] = hci
            metrics.controller_adapter_info.labels(serial=serial, adapter=hci).set(1)

    def _remove_info_series(self, serial: str) -> None:
        """Remove the backend / adapter info series of a controller that is gone.

        The discovery swap drops the serial from _serial_to_adapter before it
        calls this, so _update_controller_metrics never re-creates the series.
        """
        with self._info_series_lock:
            prev_backend = self._last_backend.pop(serial, None)
            if prev_backend:
                metrics.controller_backend_info.remove(serial, prev_backend)
            prev_hci = self._last_adapter.pop(serial, None)
            if prev_hci:
                metrics.controller_adapter_info.remove(serial, prev_hci)

    def _mark_output_sent(self, serial: str, color: tuple[int, int, int], rumble: int, now: float) -> None:
        """Record a successful write. Caller holds _led_lock."""
        self._last_sent_color[serial] = color
        self._last_sent_rumble[serial] = rumble
        self._last_led_update[serial] = now
        self._led_failures_counted.pop(serial, None)

    def _count_led_failure(self, serial: str, now: float) -> None:
        """Count a failed write at most once per keep-alive interval per serial. Caller holds _led_lock."""
        counted = self._led_failures_counted.get(serial)
        if counted is not None and now - counted < LED_KEEPALIVE_S:
            return
        self._led_failures[serial] = self._led_failures.get(serial, 0) + 1
        self._led_failures_counted[serial] = now

    def update_all_leds(self) -> int:
        """Send each controller's desired output (colour + rumble) on change or keep-alive.

        An output counts as sent only after the adapter accepted it, so a
        failed write, or a direct write skipped because the lock was busy, is
        sent on the next tick (#2355). The batch writes each controller at
        most once per tick.
        """
        current_time = time.time()
        updated_count = 0

        # list() is intentional: discovery or shutdown may modify the dict meanwhile
        # Every serial with any desired output: a rumble set before any colour
        # is retried like a colour (#2365).
        for serial in list({*self._led_colors, *self._rumble}):  # NOSONAR(S7504)
            if serial in self._effect_active or serial in self._unrouted_output:
                continue

            output = None
            try:
                # Re-read the output and adapter under the lock: discovery swaps
                # the routing table and drops stale serials under the same lock
                # (#1447), so a serial removed mid-batch is skipped, not revived.
                with self._led_lock:
                    adapter = self._serial_to_adapter.get(serial)
                    if adapter is None or (serial not in self._led_colors and serial not in self._rumble):
                        continue
                    stored_color = self._led_colors.get(serial, (0, 0, 0))

                    rumble = self._rumble.get(serial, 0)
                    output = (stored_color, rumble)
                    last_sent = (self._last_sent_color.get(serial), self._last_sent_rumble.get(serial, 0))
                    last_update = self._last_led_update.get(serial, 0)
                    output_changed = output != last_sent
                    keepalive_needed = current_time - last_update >= LED_KEEPALIVE_S
                    forced = serial in self._force_led_resend
                    if not (output_changed or keepalive_needed or forced):
                        continue

                    r, g, b = stored_color
                    if adapter.set_output(serial, r, g, b, rumble):
                        self._mark_output_sent(serial, stored_color, rumble, current_time)
                        self._force_led_resend.discard(serial)
                    else:
                        self._count_led_failure(serial, current_time)
                updated_count += 1

                if output_changed:
                    logger.debug(f"LED output changed for {serial}: {last_sent} -> {output}")

            except Exception as e:
                logger.debug(f"Error updating LED for {serial}: {e}")
                with self._led_lock:
                    if serial in self._led_colors or serial in self._rumble:
                        self._count_led_failure(serial, current_time)

        return updated_count

    def force_led_resend(self, serial: str) -> None:
        """Send the serial's output on the next LED batch even if unchanged (UC-002 BR-008, #2466).

        Called when the controller (re)registers, before the owner's colour: a write kept
        from the disconnect may go out from now on (#2471).
        """
        self._force_led_resend.add(serial)
        self._release_unrouted_output(serial)

    def _release_unrouted_output(self, serial: str) -> None:
        """Let the LED batch send the serial's output again (#2471)."""
        if serial in self._unrouted_output:
            with self._routing_lock:
                self._unrouted_output.discard(serial)

    def clear_led_output(self, serial: str) -> None:
        """Forget a silently dropped controller's desired colour and rumble (UC-002 BR-007, #2466).

        The backend still lists it, so discovery's stale-state cleanup never runs for it; without
        this the forced resend of its next registration would replay a stale game or death colour.
        Under the same locks as the discovery swap's cleanup (_led_lock, then
        _routing_lock), so a running LED batch never re-sends the dropped output (#2471).
        """
        with self._led_lock, self._routing_lock:
            self._led_colors.pop(serial, None)
            self._rumble.pop(serial, None)
            self._last_sent_color.pop(serial, None)
            self._last_sent_rumble.pop(serial, None)
            self._last_led_update.pop(serial, None)
            self._force_led_resend.discard(serial)
            self._effect_active.discard(serial)
            self._unrouted_output.discard(serial)

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
        if adapter is not None:
            adapter.consume_death_frame(serial)

    def consume_trigger_press(self, serial: str) -> bool:
        """Route the per-game-frame trigger latch read to the owning adapter (#1837)."""
        adapter = self._serial_to_adapter.get(serial)
        return adapter.consume_trigger_press(serial) is True if adapter is not None else False

    # -- Motion patterns (UC-046, #1258): only the MockAdapter implements them --

    def pattern_serials(self) -> set[str]:
        """Serials that carry a motion pattern, across adapters (cheap per-frame check)."""
        out: set[str] = set()
        for adapter in self._adapters:
            out |= adapter.pattern_serials
        return out

    def pattern_frame(self, serial: str, subscriber_id: str, explicit: bool) -> tuple[dict, bool] | None:
        """Route a delivered-frame pattern evaluation to the owning adapter."""
        adapter = self._serial_to_adapter.get(serial)
        return adapter.pattern_frame(serial, subscriber_id, explicit) if adapter is not None else None

    def pattern_stream_rate(self, serials) -> int | None:
        """Pinned stream rate when the stream names a pattern controller, else None."""
        for serial in serials:
            adapter = self._serial_to_adapter.get(serial)
            rate = adapter.pattern_stream_rate([serial]) if adapter is not None else None
            if rate is not None:
                return rate
        return None

    def record_pattern_overrun(self, subscriber_id: str) -> int:
        return sum(adapter.record_pattern_overrun(subscriber_id) for adapter in self._adapters)

    def release_pattern_stream(self, subscriber_id: str) -> None:
        for adapter in self._adapters:
            adapter.release_pattern_stream(subscriber_id)

    def _store_unrouted_output(
        self, serial: str, color: tuple[int, int, int] | None = None, rumble: int | None = None
    ) -> ControllerIOAdapter | None:
        """Keep a write for a serial with no route, or one routed again but not registered yet.

        Returns the adapter when the serial is routed and not marked (write it now).
        A write during a disconnect is held, never sent, until the controller registers
        again: ``force_led_resend`` (called before the owner's colour) releases it, and the
        owner's colour then replaces it (UC-002 BR-008, #2471). Under _routing_lock, so the
        discovery swap's stale-state cleanup either sees the mark or runs before the write.
        """
        with self._routing_lock:
            adapter = self._serial_to_adapter.get(serial)
            if self._closed or (adapter is not None and serial not in self._unrouted_output):
                return adapter
            if color is not None:
                self._led_colors[serial] = color
            if rumble is not None:
                self._rumble[serial] = rumble
            self._unrouted_output.add(serial)
            return None

    async def set_led_color(self, serial: str, r: int, g: int, b: int) -> bool:
        adapter = self._serial_to_adapter.get(serial)
        if adapter is None or serial in self._unrouted_output:
            adapter = self._store_unrouted_output(serial, color=(r, g, b))
            if adapter is None:
                return False  # held until the registration (#2471)
        self._led_colors[serial] = (r, g, b)
        rumble = self._rumble.get(serial, 0)
        # Non-blocking lock: if update_all_leds() batch is in progress, skip
        # the direct USB write to avoid concurrent set_output() calls.
        # _led_colors is already set, so the 20Hz loop picks it up within 50ms.
        if self._led_lock.acquire(blocking=False):
            try:
                result = adapter.set_output(serial, r, g, b, rumble)
                if result:
                    self._mark_output_sent(serial, (r, g, b), rumble, time.time())
                else:
                    self._count_led_failure(serial, time.time())
                return result
            finally:
                self._led_lock.release()
        return True

    async def set_rumble(self, serial: str, intensity: int) -> bool:
        adapter = self._serial_to_adapter.get(serial)
        if adapter is None or serial in self._unrouted_output:
            adapter = self._store_unrouted_output(serial, rumble=intensity)
            if adapter is None:
                return False  # held until the registration (#2471)
        self._rumble[serial] = intensity
        r, g, b = self._led_colors.get(serial, (0, 0, 0))
        # Non-blocking lock: same pattern as set_led_color() — skip direct
        # USB write if batch is in progress. Rumble + color state is stored,
        # next update_all_leds() cycle sends the combined output; a failed
        # write is not marked as sent, so that cycle retries it (#2355).
        if self._led_lock.acquire(blocking=False):
            try:
                result = adapter.set_output(serial, r, g, b, intensity)
                if result:
                    self._mark_output_sent(serial, (r, g, b), intensity, time.time())
                else:
                    self._count_led_failure(serial, time.time())
                return result
            finally:
                self._led_lock.release()
        return True

    def set_effect_active(self, serial: str, active: bool):
        if active:
            self._effect_active.add(serial)
        else:
            self._effect_active.discard(serial)
