#!/usr/bin/env python3
"""Offline calibration for the #1125 spike-survival fitness metric.

OFFLINE-ONLY (issue #1145). Runs the VM trajectory-feature helper over recorded
games (discovered from the existing per-game accel series in Prometheus/VM) and
emits a recommended threshold (`k`) for a spike-survival metric, the per-player
feature distribution, and a sanity check that the metric BITES for erratic players
(high std/peak) without firing on steady ones.

Why this exists
---------------
The earlier `std/peak <= 0.30` attempt (#1120) was scale-invariant: spiky players
have high std AND high peak, so the ratio sat ~0.15 and 0.30 almost never bit — no
arm-to-arm separation. #1125 asks for a metric that actually discriminates
spiky-eliminated from steady-survivor players using REAL recorded data.

This script evaluates two non-scale-invariant candidates against the recorded
distribution and recommends a survivor-bound `k`:

  * coefficient of variation (CV = std / mean): scale-free, but rises with both
    burst intensity and burst frequency.
  * spike-rate (upward threshold crossings of mean + k_spike*std, per minute):
    a frequency signal that ignores absolute scale entirely.

The recommended `k` is the survivor bound on the chosen discriminator, derived from
the real distribution (a percentile gap between steady survivors and erratic
players) so the bound is defensible rather than an arbitrary constant.

Usage
-----
    uv run python scripts/calibration/calibrate_spike_survival.py \
        [--lookback-hours 48] [--min-duration-s 60] [--spike-k 1.5] [--json out.json]

No arguments are required; defaults discover games from the last 48h. Degrades
clearly (non-zero exit, explicit message) when the stack has no usable data.

No-player-identity (#23): all features are keyed by (game_id, serial) for a single
game window. Nothing is persisted or correlated across sessions.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time

from vm_trajectory_features import (
    FeatureSet,
    PrometheusUnavailable,
    TrajectoryFeatures,
    compute_features_for_game,
    connect,
    discover_game_windows,
)

# A player whose magnitude std is essentially flat is "steady": their CV / spike
# behaviour is uninformative and they should never be flagged as a spike victim.
STEADY_STD_FLOOR = 0.01  # g; below this the player barely moved relative to baseline


def gather(
    lookback_hours: float,
    min_duration_s: float,
    spike_k: float,
    step_s: float,
) -> FeatureSet:
    client = connect()
    now = time.time()
    windows = discover_game_windows(
        client,
        lookback_s=lookback_hours * 3600.0,
        now=now,
        min_duration_s=min_duration_s,
    )
    fs = FeatureSet(prom_url=client.base_url, windows=windows)
    for w in windows:
        fs.features.extend(
            compute_features_for_game(client, w, step_s=step_s, spike_k=spike_k)
        )
    return fs


def _pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    # Linear interpolation percentile.
    pos = q / 100.0 * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _describe(values: list[float]) -> dict:
    vals = [v for v in values if v == v]  # drop NaN
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "min": round(min(vals), 4),
        "p25": round(_pct(vals, 25), 4),
        "median": round(statistics.median(vals), 4),
        "mean": round(statistics.mean(vals), 4),
        "p75": round(_pct(vals, 75), 4),
        "p90": round(_pct(vals, 90), 4),
        "max": round(max(vals), 4),
        "std": round(statistics.pstdev(vals), 4) if len(vals) > 1 else 0.0,
    }


def _movers(features: list[TrajectoryFeatures]) -> list[TrajectoryFeatures]:
    """Players who actually moved enough to carry a spike signal."""
    return [f for f in features if f.std >= STEADY_STD_FLOOR and f.mean > 0]


def recommend_k(features: list[TrajectoryFeatures]) -> dict:
    """Recommend a survivor-bound k on the discriminator with the best separation.

    Strategy: among players who actually moved, separate eliminated from survivors
    (when elimination data exists). Pick the discriminator (CV or spike_rate) whose
    survivor distribution sits clearly below the eliminated distribution, then set k
    at the survivor p75 (a player above it is "erratic"). When elimination labels are
    absent, fall back to an internal split: bottom-quartile-of-movers steady cohort
    vs top-quartile erratic cohort, and set k at the steady p75.
    """
    movers = _movers(features)
    result: dict = {"n_movers": len(movers), "n_total": len(features)}
    if len(movers) < 4:
        result["status"] = "insufficient-movers"
        return result

    have_labels = any(f.eliminated is not None for f in movers)
    if have_labels:
        survivors = [f for f in movers if f.eliminated is False]
        erratic = [f for f in movers if f.eliminated is True]
        basis = "elimination-labels"
    else:
        # Internal split by CV quartiles when no elimination labels exist.
        cvs = sorted(f.cv for f in movers)
        q1 = _pct(cvs, 25)
        q3 = _pct(cvs, 75)
        survivors = [f for f in movers if f.cv <= q1]
        erratic = [f for f in movers if f.cv >= q3]
        basis = "internal-cv-quartile-split"

    result["basis"] = basis
    result["n_survivors"] = len(survivors)
    result["n_erratic"] = len(erratic)
    if len(survivors) < 2 or len(erratic) < 2:
        result["status"] = "insufficient-separation-cohorts"
        return result

    candidates = {}
    for name, getter in (
        ("cv", lambda f: f.cv),
        ("spike_rate_per_min", lambda f: f.spike_rate_per_min),
    ):
        s_vals = [getter(f) for f in survivors]
        e_vals = [getter(f) for f in erratic]
        s_p75 = _pct(s_vals, 75)
        e_median = statistics.median(e_vals)
        # Separation: how far the erratic median sits above the survivor p75 bound,
        # normalised by survivor spread (a Cohen-d-like margin).
        pooled = statistics.pstdev(s_vals + e_vals) or 1e-9
        separation = (e_median - s_p75) / pooled
        candidates[name] = {
            "survivor_p75": round(s_p75, 4),
            "erratic_median": round(e_median, 4),
            "separation": round(separation, 3),
            "survivor": _describe(s_vals),
            "erratic": _describe(e_vals),
        }

    best = max(candidates, key=lambda c: candidates[c]["separation"])
    result["status"] = "ok"
    result["discriminator"] = best
    result["recommended_k"] = candidates[best]["survivor_p75"]
    result["candidates"] = candidates
    return result


def sanity_check(
    features: list[TrajectoryFeatures], discriminator: str, k: float
) -> dict:
    """Confirm the metric bites for erratic players but not steady ones.

    A "bite" = discriminator value > k (would be flagged as a spike victim). The
    check passes when steady players (low std) are almost never flagged and erratic
    movers are frequently flagged.
    """

    def value(f: TrajectoryFeatures) -> float:
        return f.cv if discriminator == "cv" else f.spike_rate_per_min

    steady = [f for f in features if f.std < STEADY_STD_FLOOR]
    movers = _movers(features)
    if not movers:
        return {"status": "no-movers"}

    steady_flagged = sum(1 for f in steady if value(f) > k)
    erratic = sorted(movers, key=value, reverse=True)
    top_quartile = erratic[: max(1, len(erratic) // 4)]
    top_flagged = sum(1 for f in top_quartile if value(f) > k)

    return {
        "discriminator": discriminator,
        "k": round(k, 4),
        "steady_players": len(steady),
        "steady_flagged": steady_flagged,
        "steady_flag_rate": round(steady_flagged / len(steady), 3) if steady else 0.0,
        "top_quartile_movers": len(top_quartile),
        "top_quartile_flagged": top_flagged,
        "top_quartile_flag_rate": round(top_flagged / len(top_quartile), 3),
        "bites_erratic": top_flagged > 0,
        "spares_steady": (steady_flagged / len(steady) if steady else 0.0) <= 0.1,
    }


def build_report(fs: FeatureSet, spike_k: float) -> dict:
    feats = fs.features
    rec = recommend_k(feats)
    report = {
        "prometheus_url": fs.prom_url,
        "spike_k_for_count": spike_k,
        "n_games": len(fs.windows),
        "n_player_records": len(feats),
        "windows": [
            {
                "game_id": w.game_id,
                "duration_s": round(w.duration_s),
                "n_serials": w.n_serials,
            }
            for w in fs.windows
        ],
        "distributions": {
            "peak": _describe([f.peak for f in feats]),
            "mean": _describe([f.mean for f in feats]),
            "std": _describe([f.std for f in feats]),
            "cv": _describe([f.cv for f in feats]),
            "std_peak": _describe([f.std_peak for f in feats]),
            "spike_rate_per_min": _describe([f.spike_rate_per_min for f in feats]),
            "slope_g_per_s": _describe([f.slope_g_per_s for f in feats]),
        },
        "recommendation": rec,
    }
    if rec.get("status") == "ok":
        report["sanity_check"] = sanity_check(
            feats, rec["discriminator"], rec["recommended_k"]
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-hours", type=float, default=48.0)
    parser.add_argument("--min-duration-s", type=float, default=60.0)
    parser.add_argument(
        "--spike-k",
        type=float,
        default=1.5,
        help="std multiplier for spike-count threshold",
    )
    parser.add_argument(
        "--step-s", type=float, default=1.0, help="query_range resolution in seconds"
    )
    parser.add_argument(
        "--json", type=str, default=None, help="write full report JSON to this path"
    )
    args = parser.parse_args(argv)

    try:
        fs = gather(args.lookback_hours, args.min_duration_s, args.spike_k, args.step_s)
    except PrometheusUnavailable as exc:
        print(f"FINDING: no Prometheus/VM endpoint reachable: {exc}", file=sys.stderr)
        print(
            "Calibration cannot run without the observability stack.", file=sys.stderr
        )
        return 2

    if not fs.windows:
        print(
            "FINDING: no game windows with sufficient duration found in the lookback window.\n"
            "         The per-game accel series exists but no full game is recorded, OR\n"
            "         retention/resolution is insufficient — a downsampling recording rule\n"
            "         is needed to calibrate. (See #1145 retention note.)",
            file=sys.stderr,
        )
        return 3

    report = build_report(fs, args.spike_k)
    out = json.dumps(report, indent=2)
    if args.json:
        with open(args.json, "w") as fh:
            fh.write(out)
        print(f"wrote {args.json}")
    print(out)

    rec = report["recommendation"]
    if rec.get("status") != "ok":
        print(
            f"\nFINDING: could not derive a recommended k ({rec.get('status')}).",
            file=sys.stderr,
        )
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
