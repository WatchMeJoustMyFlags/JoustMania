"""Per-controller connection quality from fresh-frame gaps (UC-035 BR-004, #1431).

The discovery loop records the gap between consecutive fresh frames of each
controller. This module keeps those gaps for a rolling window (10 s) and derives
the p95 gap (nearest rank), the longest gap (including the still-open gap since
the last fresh frame) and a quality class.

Thresholds are the upstream JoustMania gameplay-derived ratings: p95 gap
<= 22 ms good, <= 30 ms fair, above poor; longest gap <= 30 ms good, <= 50 ms
fair, above poor. The class is the worse of the two. Gaps are measured at the
controller manager's poll, so they are quantised to the poll interval.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

WINDOW_SECONDS = 10.0

P95_GOOD_MS = 22.0
P95_FAIR_MS = 30.0
MAX_GOOD_MS = 30.0
MAX_FAIR_MS = 50.0

GOOD = "good"
FAIR = "fair"
POOR = "poor"

# Numeric value of controller_connection_quality: higher is worse.
QUALITY_LEVEL = {GOOD: 0, FAIR: 1, POOR: 2}

# Bounds memory per controller: a 100 Hz poll yields ~1000 gaps per 10 s.
_MAX_GAPS = 4096


def _rate(value_ms: float, good_ms: float, fair_ms: float) -> str:
    if value_ms <= good_ms:
        return GOOD
    if value_ms <= fair_ms:
        return FAIR
    return POOR


def classify(p95_ms: float, max_ms: float) -> str:
    """Quality class: the worse of the p95-gap and longest-gap ratings."""
    ratings = (_rate(p95_ms, P95_GOOD_MS, P95_FAIR_MS), _rate(max_ms, MAX_GOOD_MS, MAX_FAIR_MS))
    return max(ratings, key=QUALITY_LEVEL.__getitem__)


def percentile_nearest_rank(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of an ascending, non-empty list."""
    rank = max(1, math.ceil(fraction * len(sorted_values)))
    return sorted_values[rank - 1]


@dataclass(frozen=True)
class GapStats:
    p95_ms: float
    max_ms: float
    quality: str
    samples: int


class ReportGapWindow:
    """Rolling window of fresh-frame gaps per controller serial."""

    def __init__(self, window_seconds: float = WINDOW_SECONDS) -> None:
        self.window_seconds = window_seconds
        self._gaps: dict[str, deque[tuple[float, float]]] = {}
        self._last_fresh: dict[str, float] = {}

    def record(self, serial: str, now: float) -> None:
        """Record a fresh frame of ``serial`` at ``now`` (seconds)."""
        last = self._last_fresh.get(serial)
        self._last_fresh[serial] = now
        if last is None:
            return
        gaps = self._gaps.get(serial)
        if gaps is None:
            gaps = self._gaps[serial] = deque(maxlen=_MAX_GAPS)
        gaps.append((now, (now - last) * 1000.0))

    def snapshot(self, serial: str, now: float) -> GapStats | None:
        """Gap statistics over the window ending at ``now``; None below two fresh frames."""
        gaps = self._gaps.get(serial)
        if not gaps:
            return None
        cutoff = now - self.window_seconds
        while gaps and gaps[0][0] < cutoff:
            gaps.popleft()
        if not gaps:
            return None
        values = sorted(gap for _, gap in gaps)
        p95_ms = percentile_nearest_rank(values, 0.95)
        open_gap_ms = max(0.0, (now - self._last_fresh[serial]) * 1000.0)
        max_ms = max(values[-1], open_gap_ms)
        return GapStats(p95_ms=p95_ms, max_ms=max_ms, quality=classify(p95_ms, max_ms), samples=len(values))

    def serials(self) -> list[str]:
        return list(self._last_fresh)

    def forget(self, serial: str) -> None:
        self._gaps.pop(serial, None)
        self._last_fresh.pop(serial, None)
