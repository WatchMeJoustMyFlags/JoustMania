#!/usr/bin/env python3
"""Offline VictoriaMetrics/Prometheus acceleration trajectory-feature query helper.

This is an OFFLINE-ONLY tool (issue #1145). It is NOT imported by, and must never
be imported by, the live agent decision loop or the live fitness path: the live
agent stays Pi-safe and has no dependency on VictoriaMetrics/Prometheus. This
script only talks to the observability stack over HTTP using the Python standard
library (urllib) so it carries zero extra runtime dependencies.

What it does
------------
Given a game's ``game_id`` and a ``[start, end]`` time window, it range-queries the
per-player, per-game acceleration series that the game-coordinator already exports
(``game_player_accel_magnitude{game_id, serial}``) and computes a set of trajectory
features for every player in the game:

    peak        - max acceleration magnitude over the window (g)
    mean        - mean acceleration magnitude (g)
    std         - population standard deviation (g)
    cv          - coefficient of variation (std / mean) -- scale-INVARIANT-free spike signal
    std_peak    - std / peak (the legacy #1120 ratio, kept for comparison)
    spike_count - number of upward threshold crossings (mean + k*std), i.e. discrete spikes
    spike_rate  - spike_count per minute
    slope       - least-squares slope of magnitude vs. time (g / s), trend over the game
    n_samples   - raw sample count actually returned

No-player-identity (#23): everything is keyed by ``(game_id, serial)`` and scoped to
a single game window. Nothing is persisted or correlated across sessions.

MOCK-SUBSTRATE CAVEAT: the features here are only as meaningful as the underlying
movement. On synthetic / mock controllers (the data predominantly available today)
these features describe an artificial smear, not real play, so they are NOT real
calibration — any downstream "separation" derived from them must be treated as
provisional until recomputed on elimination-labeled real-hardware games (#1017).
"""

from __future__ import annotations

import json
import statistics
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# The acceleration series exported per (game_id, serial) by the game-coordinator.
ACCEL_METRIC = "game_player_accel_magnitude"
# Series used to discover game windows (per game_id, emitted while a game is active).
GAME_WINDOW_METRIC = "game_player_accel_magnitude"
# Survivor vs eliminated signal (per game_id, serial); presence/value encodes order.
ELIMINATION_METRIC = "game_player_elimination_order"

DEFAULT_PROM_URLS = (
    "http://localhost/prometheus",
    "http://localhost:8080/prometheus",
)


class PrometheusUnavailable(RuntimeError):
    """Raised when no Prometheus/VM endpoint responds (degrade clearly, never crash)."""


@dataclass
class TrajectoryFeatures:
    """Per-player trajectory features over one game window. Session-scoped only."""

    game_id: str
    serial: str
    n_samples: int
    peak: float
    mean: float
    std: float
    cv: float
    std_peak: float
    spike_count: int
    spike_rate_per_min: float
    slope_g_per_s: float
    eliminated: bool | None = None  # None when elimination data is unavailable

    def as_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "serial": self.serial,
            "n_samples": self.n_samples,
            "peak": round(self.peak, 4),
            "mean": round(self.mean, 4),
            "std": round(self.std, 4),
            "cv": round(self.cv, 4),
            "std_peak": round(self.std_peak, 4),
            "spike_count": self.spike_count,
            "spike_rate_per_min": round(self.spike_rate_per_min, 3),
            "slope_g_per_s": round(self.slope_g_per_s, 6),
            "eliminated": self.eliminated,
        }


@dataclass
class GameWindow:
    """A discovered game's time window. Session-scoped, keyed by game_id only."""

    game_id: str
    start: float
    end: float
    n_serials: int = 0

    @property
    def duration_s(self) -> float:
        return self.end - self.start


class PromClient:
    """Minimal read-only Prometheus HTTP client (stdlib only, offline use)."""

    def __init__(self, base_url: str, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 (trusted local URL)
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("status") != "success":
            raise PrometheusUnavailable(
                f"query failed: {payload.get('error', payload)}"
            )
        return payload["data"]

    def query_range(
        self, promql: str, start: float, end: float, step: float
    ) -> list[dict]:
        data = self._get(
            "/api/v1/query_range",
            {
                "query": promql,
                "start": f"{start:.3f}",
                "end": f"{end:.3f}",
                "step": f"{step:g}",
            },
        )
        return data.get("result", [])

    def query(self, promql: str) -> list[dict]:
        return self._get("/api/v1/query", {"query": promql}).get("result", [])

    def reachable(self) -> bool:
        try:
            self.query("vector(1)")
            return True
        except (urllib.error.URLError, PrometheusUnavailable, OSError):
            return False


def connect(
    urls: tuple[str, ...] = DEFAULT_PROM_URLS, timeout: float = 20.0
) -> PromClient:
    """Return the first reachable Prometheus/VM client, or raise PrometheusUnavailable."""
    tried = []
    for url in urls:
        client = PromClient(url, timeout=timeout)
        if client.reachable():
            return client
        tried.append(url)
    raise PrometheusUnavailable(
        f"no Prometheus endpoint reachable (tried: {', '.join(tried)})"
    )


def _escape(label_value: str) -> str:
    return label_value.replace("\\", "\\\\").replace('"', '\\"')


def discover_game_windows(
    client: PromClient,
    lookback_s: float,
    now: float,
    min_duration_s: float = 30.0,
    probe_step_s: float = 30.0,
) -> list[GameWindow]:
    """Discover game windows from the per-game accel series over the last lookback_s.

    A "window" is the first..last timestamp at which the game emitted accel samples.
    Games shorter than min_duration_s (e.g. single-scrape shadow probes) are dropped.

    Assumption: all samples sharing a game_id belong to one contiguous game and are
    stitched into a single first..last window. A reused game_id (or a gap-separated
    re-run under the same id) would be merged into one oversized window.
    """
    start = now - lookback_s
    # count_over_time collapses each series to presence; group by game_id below.
    promql = f"count_over_time({GAME_WINDOW_METRIC}[{int(probe_step_s)}s]) > 2"
    series = client.query_range(promql, start, now, probe_step_s)
    by_game: dict[str, list[float]] = {}
    serials: dict[str, set[str]] = {}
    for s in series:
        gid = s["metric"].get("game_id")
        if not gid:
            continue
        ts = [float(t) for t, _ in s["values"]]
        by_game.setdefault(gid, []).extend(ts)
        serials.setdefault(gid, set()).add(s["metric"].get("serial", ""))
    windows = []
    for gid, ts in by_game.items():
        ts.sort()
        win = GameWindow(
            game_id=gid, start=ts[0], end=ts[-1], n_serials=len(serials[gid])
        )
        if win.duration_s >= min_duration_s:
            windows.append(win)
    windows.sort(key=lambda w: w.start)
    return windows


def _eliminated_serials(client: PromClient, game_id: str) -> set[str] | None:
    """Return serials that were eliminated in a game, or None if unavailable.

    A non-zero game_player_elimination_order means the player was eliminated;
    survivors either have order 0 or no elimination sample. Returns None when the
    elimination metric is absent for this game so callers can mark eliminated=None.
    """
    promql = f'last_over_time({ELIMINATION_METRIC}{{game_id="{_escape(game_id)}"}}[6h])'
    try:
        result = client.query(promql)
    except (urllib.error.URLError, PrometheusUnavailable, OSError):
        return None
    if not result:
        return None
    out: set[str] = set()
    for s in result:
        serial = s["metric"].get("serial")
        try:
            order = float(s["value"][1])
        except (TypeError, ValueError, IndexError):
            continue
        if serial and order > 0:
            out.add(serial)
    return out


def _spike_count(values: list[float], k: float) -> int:
    """Count upward threshold crossings of (mean + k*std).

    A spike is an UPWARD crossing: a sample at/above the threshold whose previous
    sample was below it. This counts discrete bursts, not the number of high
    samples, so a sustained-high steady player does not rack up spikes.
    """
    if len(values) < 2:
        return 0
    mean = statistics.mean(values)
    std = statistics.pstdev(values)
    if std == 0:
        return 0
    threshold = mean + k * std
    count = 0
    prev_above = values[0] >= threshold
    for v in values[1:]:
        above = v >= threshold
        if above and not prev_above:
            count += 1
        prev_above = above
    return count


def _slope(times: list[float], values: list[float]) -> float:
    """Least-squares slope of value vs. time (g/s). 0 when degenerate."""
    n = len(values)
    if n < 2:
        return 0.0
    t0 = times[0]
    xs = [t - t0 for t in times]
    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(values)
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values, strict=True))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return num / den


def compute_features_for_game(
    client: PromClient,
    window: GameWindow,
    step_s: float = 1.0,
    spike_k: float = 1.5,
) -> list[TrajectoryFeatures]:
    """Range-query the per-player accel series for a game window and compute features.

    spike_k is the std-multiplier used for the spike-count threshold (mean + k*std).
    step_s is the query_range resolution; the native otel push interval is ~0.5s, so
    step_s<=1.0 preserves the waveform.
    """
    promql = f'{ACCEL_METRIC}{{game_id="{_escape(window.game_id)}"}}'
    # Pad the window slightly so edge samples are not clipped.
    series = client.query_range(
        promql, window.start - step_s, window.end + step_s, step_s
    )
    eliminated = _eliminated_serials(client, window.game_id)
    out: list[TrajectoryFeatures] = []
    for s in series:
        serial = s["metric"].get("serial", "")
        pts = s["values"]
        values = [float(v) for _, v in pts]
        times = [float(t) for t, _ in pts]
        if not values:
            continue
        peak = max(values)
        mean = statistics.mean(values)
        std = statistics.pstdev(values)
        cv = std / mean if mean > 0 else 0.0
        std_peak = std / peak if peak > 0 else 0.0
        spikes = _spike_count(values, spike_k)
        duration_min = max((times[-1] - times[0]) / 60.0, 1e-9)
        out.append(
            TrajectoryFeatures(
                game_id=window.game_id,
                serial=serial,
                n_samples=len(values),
                peak=peak,
                mean=mean,
                std=std,
                cv=cv,
                std_peak=std_peak,
                spike_count=spikes,
                spike_rate_per_min=spikes / duration_min,
                slope_g_per_s=_slope(times, values),
                eliminated=(serial in eliminated) if eliminated is not None else None,
            )
        )
    out.sort(key=lambda f: f.serial)
    return out


@dataclass
class FeatureSet:
    """All per-player features across all discovered games. Session-scoped batch."""

    prom_url: str
    windows: list[GameWindow] = field(default_factory=list)
    features: list[TrajectoryFeatures] = field(default_factory=list)
