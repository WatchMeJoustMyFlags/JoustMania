"""
UnstableAdapter — a deliberately-degraded ControllerIOAdapter decorator.

This adapter exists to give M3 a *routable* backend that reliably violates the
perception fitness thresholds (movement update rate, dropped-event ratio, event
gap). It is selected only via explicit ``bluetooth_backend`` targeting or via the
``rollout`` domain — never as a default/fallback winner.

Unlike :class:`ChaosAdapter`, whose ``adapter_type`` is *transparent* (it returns
the inner adapter's type so routing treats a chaos-wrapped python adapter as
"python"), ``UnstableAdapter.adapter_type`` returns ``"unstable"`` so the
multiplexer can route specific serials to it while other serials continue to use
the plain inner adapter.

Degradation is fully DETERMINISTIC (no ``random``) so tests can assert exact
threshold violations:

  * Update rate < 10 Hz — ``poll()`` serves a *fresh* inner result at most once
    every ``throttle_seconds`` (default 0.12 s ≈ 8.3 Hz). Between throttle windows
    it replays the last served frame (a stale repeat), so the perceived rate of
    genuinely-new frames stays under 10 Hz.
  * Event gap > 50 ms — a direct consequence of the 120 ms throttle window: the
    time between two fresh frames is always ≥ 120 ms > 50 ms.
  * Dropped-event ratio > 2% — every ``drop_every``-th fresh frame (default 10,
    i.e. 10%) is dropped by returning ``None`` instead of the inner result.

``poll()`` MUST NOT sleep or block: it runs inside the shared poll batch.
Degradation is achieved purely by throttling/dropping using a monotonic clock —
never by blocking.

Shared-inner design (the simultaneous-routability question)
-----------------------------------------------------------
During a progressive rollout, "python" and "unstable" must be routable
*simultaneously* for different serials. ``UnstableAdapter`` therefore wraps the
**same** :class:`PythonHidAdapter` instance that the plain "python" route uses
rather than opening a second, independent one. Evidence this is the only safe
option:

  * ``PythonHidAdapter.open``/``close`` are unary RPCs against *global* device
    state in the shared python-hid service. Two independent ``PythonHidAdapter``
    instances both connect to that one service, so a ``Close`` issued by one
    tears the device down for the other. The multiplexer routinely closes the
    *losing* adapter for a serial, which would tear down the device the winner
    is actively using.
  * ``PythonHidAdapter.poll`` is destructive (it ``pop``\\s ``_latest_data``),
    so two clients would duplicate-and-compete over the same stream.
  * The multiplexer guarantees exactly one winner per serial, so with a shared
    inner only the winner's ``open``/``poll``/``set_output`` reach the device —
    there is no contention.

Because the inner is shared, ``UnstableAdapter`` is registered ONLY in the
multiplexer's ``_adapter_by_type`` map (as a routing target); it is *not* added
as an independent discoverer. ``discover``/``open``/``close``/``close_all`` are
intentionally pass-throughs to the shared inner so the discoverer (the plain
python adapter) and the unstable wrapper agree on device lifecycle. To keep the
multiplexer from closing the shared device when a serial switches between the
plain and unstable routes, the wrapper exposes :pyattr:`inner` and the
multiplexer skips closing any losing adapter that shares the winner's inner.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from services.controller_manager.multiplexer.adapter import ControllerIOAdapter

logger = logging.getLogger(__name__)

# Defaults chosen so the M3 fitness thresholds are reliably violated:
#   throttle 0.12 s -> ~8.3 Hz fresh frames (< 10 Hz) and 120 ms gaps (> 50 ms)
#   drop every 10th fresh frame -> 10% drop ratio (> 2%)
DEFAULT_THROTTLE_SECONDS = 0.12
DEFAULT_DROP_EVERY = 10


class UnstableAdapter(ControllerIOAdapter):
    """Deterministically-degraded decorator around a shared inner adapter.

    Args:
        inner: The adapter to delegate to (shared with the plain route).
        throttle_seconds: Minimum spacing between *fresh* inner polls.
        drop_every: Every Nth fresh poll is dropped (returns None). Must be >= 2
            to keep the drop ratio finite and above the 2% threshold.
        time_source: Monotonic clock injection point for tests.
    """

    def __init__(
        self,
        inner: ControllerIOAdapter,
        throttle_seconds: float = DEFAULT_THROTTLE_SECONDS,
        drop_every: int = DEFAULT_DROP_EVERY,
        time_source: Callable[[], float] = time.monotonic,
    ):
        if drop_every < 2:
            raise ValueError("drop_every must be >= 2")
        self._inner = inner
        self._throttle_seconds = throttle_seconds
        self._drop_every = drop_every
        self._now = time_source

        # Per-serial throttle bookkeeping.
        self._last_fresh_time: dict[str, float] = {}
        self._last_result: dict[str, dict | None] = {}
        self._fresh_count: dict[str, int] = {}

    @property
    def adapter_type(self) -> str:
        # NOT transparent (unlike ChaosAdapter): routing must see "unstable".
        return "unstable"

    @property
    def inner(self) -> ControllerIOAdapter:
        """The shared inner adapter (used by the multiplexer for dedup safety)."""
        return self._inner

    # -- Lifecycle: pass straight through to the shared inner -----------------

    def discover(
        self,
        force: bool = False,
        verify_only: bool = False,
        exclude_serials: list[str] | None = None,
    ) -> list[str]:
        return self._inner.discover(
            force=force,
            verify_only=verify_only,
            exclude_serials=exclude_serials,
        )

    def open(self, serial: str) -> bool:
        return self._inner.open(serial)

    def set_output(self, serial: str, r: int, g: int, b: int, rumble: int) -> bool:
        return self._inner.set_output(serial, r, g, b, rumble)

    def get_adapter_for_serial(self, serial: str) -> str | None:
        return self._inner.get_adapter_for_serial(serial)

    def close(self, serial: str) -> None:
        self._inner.close(serial)
        self._last_fresh_time.pop(serial, None)
        self._last_result.pop(serial, None)
        self._fresh_count.pop(serial, None)

    def close_all(self) -> None:
        self._inner.close_all()
        self._last_fresh_time.clear()
        self._last_result.clear()
        self._fresh_count.clear()

    # -- Degraded poll --------------------------------------------------------

    def poll(self, serial: str) -> dict | None:
        """Throttle + deterministically drop. Never blocks.

        Within a throttle window the last served frame is replayed (a stale
        repeat) so the rate of *fresh* frames stays under 10 Hz and the gap
        between fresh frames stays above 50 ms. Every ``drop_every``-th fresh
        frame is dropped (returns None) to push the drop ratio above 2%.
        """
        now = self._now()
        last_fresh = self._last_fresh_time.get(serial)

        if last_fresh is not None and (now - last_fresh) < self._throttle_seconds:
            # Inside the throttle window: replay the last served frame unchanged.
            return self._last_result.get(serial)

        # Throttle window elapsed -> serve a fresh frame.
        self._last_fresh_time[serial] = now
        count = self._fresh_count.get(serial, 0) + 1
        self._fresh_count[serial] = count

        result = self._inner.poll(serial)

        # Deterministic drop: every Nth fresh frame is dropped.
        if count % self._drop_every == 0:
            self._last_result[serial] = None
            return None

        self._last_result[serial] = result
        return result

    def __getattr__(self, name: str) -> Any:
        """Delegate unknown attributes to the inner adapter (mirrors ChaosAdapter)."""
        return getattr(self._inner, name)
