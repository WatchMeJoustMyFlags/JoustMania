#!/usr/bin/env python3
"""Collect and report for scripts/agent-shadow-realism.sh (#1017, UC-043 A22).

Compares REAL games with SHADOW games over a time window, from the observability stack only:
the VictoriaMetrics Prometheus query API (every metric) and, optionally, Jaeger's query API v3
(a cross-check of the near-death count from the coordinator's ``player_analytics`` span events).
Read-only: it sends GET requests to those two APIs and nothing else (no flag write, no game start).

Stdlib only. Subcommands:

  collect --vm-url U --start EPOCH --end EPOCH [--jaeger-url U] [--mode M]
                    prints the dataset (JSON): one record per game, one per player
  report --dataset F [--min-n 8] [--ks-max 0.3] [--ratio-max 1.5] [--json]
                    per metric and kind: n, median, quartiles; the distance between the kinds
                    (two-sample KS statistic, ratio of medians shadow / real) and a verdict

Verdict per metric (counts are games; a player metric is one value per game, the mean over its
players): ``insufficient`` when either kind has fewer than --min-n games or the KS critical value
at alpha 0.05 for the two game counts is above --ks-max (the sample cannot resolve that gap), otherwise
``similar`` when KS <= --ks-max AND the ratio of medians is inside [1/--ratio-max, --ratio-max],
otherwise ``divergent``. ``insufficient`` is never ``similar`` (fail closed for the validity bar,
UC-043 A23). The per-game definitions approximate decide-contract §14.1 from 1 Hz gauges; they are
listed in docs/agent-shadow-realism-runbook.md.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

STEP_S = 1  # the coordinator exports the per-player gauges about once a second
END_MARGIN_S = 120  # final exports of a game that ends near the window's end
# The near-death counter is incremented once, at game end, and exported within about a second:
# a game's count is the increase over (end + AFTER - WINDOW, end + AFTER].
NEAR_DEATH_AFTER_S = 5
NEAR_DEATH_WINDOW_S = 10
NEAR_DEATH_LAG_S = 2  # export lag of another game's increment

# Metric name, unit, sample unit (game or player), source of the samples.
METRICS = [
    ("duration", "s", "game"),
    ("elimination_pace", "1/min", "game"),
    ("near_death_rate", "1/player-min", "game"),
    ("near_death_rate_jaeger", "1/player-min", "game"),
    ("activity", "x threshold", "game"),
    ("change", "x threshold", "game"),
    ("closeness", "x threshold", "game"),
    ("survival", "fraction", "game"),
    ("skill_gap", "skill", "game"),
    ("intensity", "g", "player"),  # per game: mean over its players
    ("movement_variance", "g^2", "player"),
]
# The metrics each objective's fitness reads (services/agent/decision/fitness.go).
OBJECTIVE_INPUTS = {
    "endurance": ["duration"],
    "accelerate": ["duration"],
    "balanced": ["skill_gap"],
}


class QueryError(Exception):
    pass


# ------------------------------------------------------------------ HTTP (GET only)


def _get(url: str, timeout: float = 30):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise QueryError(f"GET {url.split('?')[0]}: HTTP {err.code}") from err
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise QueryError(f"GET {url.split('?')[0]}: {err}") from err


class VM:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def instant(self, expr: str, at: float) -> list[dict]:
        url = f"{self.base}/api/v1/query?" + urllib.parse.urlencode(
            {"query": expr, "time": f"{at:.3f}"}
        )
        doc = _get(url)
        if not doc or doc.get("status") != "success":
            raise QueryError(f"query failed: {expr}")
        return doc["data"]["result"]

    def range(
        self, expr: str, start: float, end: float, step: int = STEP_S
    ) -> list[dict]:
        params = {
            "query": expr,
            "start": f"{start:.3f}",
            "end": f"{end:.3f}",
            "step": str(step),
        }
        doc = _get(f"{self.base}/api/v1/query_range?" + urllib.parse.urlencode(params))
        if not doc or doc.get("status") != "success":
            raise QueryError(f"query_range failed: {expr}")
        return doc["data"]["result"]


def _kind(label: str) -> str | None:
    """The coordinator's game_kind label in the flag vocabulary (telemetry.md: primary = real)."""
    return {"primary": "real", "real": "real", "shadow": "shadow"}.get(label or "")


# ------------------------------------------------------------------ per-game measures


def _grid(series: list[dict]) -> dict[str, dict[int, float]]:
    """serial -> {second: value} from a query_range answer."""
    out: dict[str, dict[int, float]] = {}
    for s in series:
        serial = s["metric"].get("serial", "?")
        out[serial] = {int(float(t)): float(v) for t, v in s.get("values") or []}
    return out


def _alive_at(
    alive: dict[str, dict[int, float]], serial: str, t: int, elim_ts: dict[str, float]
) -> bool:
    if serial in elim_ts and t >= elim_ts[serial]:
        return False
    vals = alive.get(serial)
    if not vals:
        return True
    known = [k for k in vals if k <= t]
    return True if not known else vals[max(known)] >= 0.5


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def game_measures(accel, alive, elim_ts, threshold, start, end):
    """activity, change, closeness (decide-contract §14.1, sampled from the 1 Hz gauges)."""
    if not accel or not threshold or threshold <= 0:
        return {"activity": None, "change": None, "closeness": None}
    secs = range(math.ceil(start), int(end) + 1)
    norm = {s: {t: v / threshold for t, v in vals.items()} for s, vals in accel.items()}
    field: dict[int, float] = {}
    for t in secs:
        vals = [
            norm[s][t] for s in norm if t in norm[s] and _alive_at(alive, s, t, elim_ts)
        ]
        if len(vals) >= 2:
            field[t] = sum(vals) / len(vals)
    activity = _mean(field.values())

    def sub_mean(
        a, b
    ):  # mean field activity over (a, b], at least half the seconds sampled
        vals = [field[t] for t in range(a + 1, b + 1) if t in field]
        return sum(vals) / len(vals) if len(vals) * 2 >= (b - a) else None

    swings = []
    t0 = math.ceil(start)
    for t in range(t0 + 15, int(end) + 1, 5):
        subs = [sub_mean(t - 15, t - 10), sub_mean(t - 10, t - 5), sub_mean(t - 5, t)]
        if all(x is not None for x in subs):
            swings.append(max(subs) - min(subs))

    spreads = []
    for t in range(t0 + 10, int(end) + 1, 5):
        levels = []
        for s, series in norm.items():
            if not _alive_at(alive, s, t, elim_ts):
                continue
            vals = [series[k] for k in range(t - 9, t + 1) if k in series]
            if len(vals) >= 5:
                levels.append(sum(vals) / len(vals))
        if len(levels) >= 2:
            spreads.append(max(levels) - min(levels))
    return {"activity": activity, "change": _mean(swings), "closeness": _mean(spreads)}


def survival(serials, elim_ts, start, duration):
    if not serials or duration <= 0:
        return None
    fr = []
    for s in serials:
        if s in elim_ts:
            fr.append(min(1.0, max(0.0, (elim_ts[s] - start) / duration)))
        else:
            fr.append(1.0)
    return sum(fr) / len(fr)


# ------------------------------------------------------------------ Jaeger (optional)


def _scalar(v: dict):
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in v:
            return v[key]
    return None


def jaeger_near_deaths(base: str, game_id: str, start: float, end: float) -> int | None:
    """Sum of analytics.near_death_count over the game's player_analytics events; None = no trace."""
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    params = {
        "query.serviceName": "game-coordinator-service",
        "query.attributes": json.dumps({"game.id": game_id}),
        "query.searchDepth": "20",
        "query.startTimeMin": time.strftime(fmt, time.gmtime(start - 600)),
        "query.startTimeMax": time.strftime(fmt, time.gmtime(end + 600)),
    }
    doc = _get(
        base.rstrip("/") + "/v3/traces?" + urllib.parse.urlencode(params), timeout=60
    )
    if not doc:
        return None
    per_serial: dict[
        str, int
    ] = {}  # one summary per player; a copy on another span is not counted twice
    for rs in doc.get("result", doc).get("resourceSpans") or []:
        for ss in rs.get("scopeSpans") or []:
            for sp in ss.get("spans") or []:
                for ev in sp.get("events") or []:
                    if ev.get("name") != "player_analytics":
                        continue
                    attrs = {
                        a["key"]: _scalar(a.get("value") or {})
                        for a in ev.get("attributes") or []
                    }
                    if attrs.get("game_id", game_id) != game_id:
                        continue
                    count = attrs.get(
                        "near_death_count", attrs.get("analytics.near_death_count")
                    )
                    try:
                        per_serial[str(attrs.get("serial", len(per_serial)))] = int(
                            float(count)
                        )
                    except (TypeError, ValueError):
                        continue
    total, seen = sum(per_serial.values()), bool(per_serial)
    return total if seen else None


# ------------------------------------------------------------------ collect


def _by_game(result: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in result:
        out[r["metric"].get("game_id", "")].append(r)
    return out


def _near_deaths(
    vm: VM, games: list[dict], game_serials: dict, skipped: Counter, now: float
) -> None:
    """Per-game near-death count from game_near_death_events_total (labels serial, game_mode).

    The counter has no game_id: the coordinator adds each player's count once, at game end. So the
    count of a game is the increase over its serials in a short window around its end. A game that
    shares a serial with another game whose increment falls in that window (concurrent shadow games reuse the
    mock serials) cannot be told apart: it gets no value and is counted as skipped.
    """
    lo = NEAR_DEATH_AFTER_S - NEAR_DEATH_WINDOW_S - NEAR_DEATH_LAG_S
    hi = NEAR_DEATH_AFTER_S
    for g in games:
        serials = game_serials.get(g["game_id"], [])
        if not serials or g["duration"] <= 0:
            continue
        mine = set(serials)
        clash = any(
            o is not g
            and lo <= o["end"] - g["end"] <= hi
            and mine & set(game_serials.get(o["game_id"], []))
            for o in games
        )
        if clash:
            skipped["near_death_shared_serials"] += 1
            continue
        sel = "|".join(serials)
        res = vm.instant(
            f'sum(increase(game_near_death_events_total{{serial=~"{sel}"}}[{NEAR_DEATH_WINDOW_S}s]))',
            min(now, g["end"] + NEAR_DEATH_AFTER_S),
        )
        if res:
            g["near_deaths"] = round(float(res[0]["value"][1]), 3)
            g["near_death_rate"] = g["near_deaths"] / (
                len(serials) * g["duration"] / 60
            )


def collect(
    vm: VM,
    start: float,
    end: float,
    jaeger: str | None = None,
    mode: str | None = None,
    now=None,
) -> dict:
    now = time.time() if now is None else now
    at = min(now, end + END_MARGIN_S)
    look = f"{int(at - start) + 900}s"  # games that started before the window and ended in it
    durations = vm.instant(f"last_over_time(game_duration_seconds[{look}])", at)
    # The duration gauge can outlive its game (a stale series is exported for hours with the final
    # value), so the game is placed by its FIRST sample: start = first timestamp - first value.
    firsts = {
        r["metric"].get("game_id"): float(r["value"][1])
        for r in vm.instant(f"tfirst_over_time(game_duration_seconds[{look}])", at)
    }
    first_vals = {
        r["metric"].get("game_id"): float(r["value"][1])
        for r in vm.instant(f"first_over_time(game_duration_seconds[{look}])", at)
    }
    modes = {
        r["metric"].get("game_id"): r["metric"].get("mode", "")
        for r in vm.instant(f"last_over_time(game_current_mode[{look}])", at)
    }
    thresholds = {
        r["metric"].get("game_id"): float(r["value"][1])
        for r in vm.instant(
            f"last_over_time(game_effective_death_threshold[{look}])", at
        )
    }
    elims = _by_game(
        vm.instant(f"tfirst_over_time(game_player_elimination_order[{look}])", at)
    )
    variances = _by_game(
        vm.instant(f"last_over_time(game_player_movement_variance[{look}])", at)
    )
    skills = _by_game(
        vm.instant(f"last_over_time(game_player_skill_level[{look}])", at)
    )

    games, players, skipped = [], [], Counter()
    game_serials: dict[str, list[str]] = {}
    for r in durations:
        gid = r["metric"].get("game_id", "")
        kind = _kind(r["metric"].get("game_kind", ""))
        dur = float(r["value"][1])
        g_end = firsts[gid] - first_vals.get(gid, 0.0) + dur if gid in firsts else None
        if kind is None:
            skipped["unknown_kind"] += 1
            continue
        if g_end is None or not (start <= g_end <= end):
            continue
        g_mode = modes.get(gid, "")
        if mode and g_mode != mode:
            skipped["other_mode"] += 1
            continue
        g_start = g_end - dur
        accel = _grid(
            vm.range(f'game_player_accel_magnitude{{game_id="{gid}"}}', g_start, g_end)
        )
        alive = _grid(vm.range(f'game_player_alive{{game_id="{gid}"}}', g_start, g_end))
        elim_ts = {
            e["metric"].get("serial", "?"): float(e["value"][1])
            for e in elims.get(gid, [])
        }
        serials = sorted(set(accel) | set(alive) | set(elim_ts))
        m = game_measures(accel, alive, elim_ts, thresholds.get(gid), g_start, g_end)
        rec = {
            "game_id": gid,
            "kind": kind,
            "mode": g_mode,
            "experiment_id": r["metric"].get("experiment_id", ""),
            "arm": r["metric"].get("arm", ""),
            "start": round(g_start, 3),
            "end": round(g_end, 3),
            "duration": round(dur, 3),
            "players": len(serials),
            "eliminations": len(elim_ts),
            "elimination_pace": len(elim_ts) / (dur / 60) if dur > 0 else None,
            "survival": survival(serials, elim_ts, g_start, dur),
            **m,
        }
        levels = [float(s["value"][1]) for s in skills.get(gid, [])]
        rec["skill_gap"] = max(levels) - min(levels) if len(levels) >= 2 else None
        rec["near_deaths"] = rec["near_death_rate"] = None
        game_serials[gid] = serials
        rec["near_death_rate_jaeger"] = None
        if jaeger:
            nd = jaeger_near_deaths(jaeger, gid, g_start, g_end)
            if nd is not None and serials and dur > 0:
                rec["near_death_rate_jaeger"] = nd / (len(serials) * dur / 60)
        games.append(rec)
        var_by = {
            v["metric"].get("serial"): float(v["value"][1])
            for v in variances.get(gid, [])
        }
        for s in serials:
            vals = list(accel.get(s, {}).values())
            players.append(
                {
                    "game_id": gid,
                    "kind": kind,
                    "serial": s,
                    "intensity": sum(vals) / len(vals) if vals else None,
                    "movement_variance": var_by.get(s),
                }
            )

    _near_deaths(vm, games, game_serials, skipped, now)

    # Deaths per kind from the games' eliminations, not from game_player_deaths_total: that counter
    # has in-series resets on the stage Pi (two writers on one series identity, and restarts that
    # keep the container id), so neither increase() nor max-minus-before is trustworthy (see the
    # coordinator counter-reset issue). In FFA every death is an elimination; respawn modes
    # (NonStop, Zombies) die more often than they eliminate, so compare them with --mode only.
    aggregate: dict[str, dict] = {}
    for kind in ("real", "shadow"):
        mine = [g for g in games if g["kind"] == kind]
        aggregate[kind] = {
            "deaths_total": float(sum(g["eliminations"] for g in mine)),
            "game_minutes": round(sum(g["duration"] for g in mine) / 60, 3),
        }
    return {
        "window": {"start": start, "end": end},
        "mode": mode or "",
        "jaeger": bool(jaeger),
        "skipped": dict(skipped),
        "games": games,
        "players": players,
        "aggregate": aggregate,
    }


# ------------------------------------------------------------------ statistics


def quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def ks_statistic(a: list[float], b: list[float]) -> float:
    """Two-sample Kolmogorov-Smirnov statistic: the largest gap between the two empirical CDFs."""
    sa, sb = sorted(a), sorted(b)
    i = j = 0
    d = 0.0
    while i < len(sa) and j < len(sb):
        x = min(sa[i], sb[j])
        while i < len(sa) and sa[i] <= x:
            i += 1
        while j < len(sb) and sb[j] <= x:
            j += 1
        d = max(d, abs(i / len(sa) - j / len(sb)))
    return d


def median_ratio(real_med: float, shadow_med: float) -> float:
    if real_med == 0 and shadow_med == 0:
        return 1.0
    if real_med == 0:
        return math.inf
    return shadow_med / real_med


KS_C_ALPHA = 1.358  # two-sample KS critical coefficient at alpha 0.05


def ks_critical(n: int, m: int) -> float:
    """The KS statistic two samples of n and m games need to differ at alpha 0.05."""
    return KS_C_ALPHA * math.sqrt((n + m) / (n * m)) if n and m else math.inf


def verdict(n_real, n_shadow, ks, ratio, min_n, ks_max, ratio_max) -> str:
    """Counts are GAMES. Insufficient below min_n games of a kind, and while the sample cannot
    resolve a gap of ks_max (its KS critical value at alpha 0.05 is above ks_max: about 27 shadow
    games against 89 real, 41 per kind when equal), so neither similar nor divergent is noise."""
    if n_real < min_n or n_shadow < min_n or ks_critical(n_real, n_shadow) > ks_max:
        return "insufficient"
    if ks is not None and ks > ks_max:
        return "divergent"
    if not (1 / ratio_max <= ratio <= ratio_max):
        return "divergent"
    return "similar"


def _per_game(players: list[dict], name: str) -> list[dict]:
    """A player-level metric as one value per game (the mean over its players): the players of
    one game are not independent samples, so every verdict counts games."""
    by: dict[tuple, list[float]] = defaultdict(list)
    for p in players:
        if p.get(name) is not None:
            by[(p["game_id"], p["kind"])].append(p[name])
    return [{"kind": k, name: sum(v) / len(v)} for (_, k), v in by.items()]


def compare(
    dataset: dict, min_n: int = 8, ks_max: float = 0.3, ratio_max: float = 1.5
) -> dict:
    rows = []
    for name, unit, level in METRICS:
        if name == "near_death_rate_jaeger" and not dataset.get("jaeger"):
            continue
        src = (
            dataset["games"] if level == "game" else _per_game(dataset["players"], name)
        )
        samples = {
            k: [r[name] for r in src if r["kind"] == k and r.get(name) is not None]
            for k in ("real", "shadow")
        }
        row = {"metric": name, "unit": unit, "level": level}
        for k, xs in samples.items():
            row[k] = (
                {
                    "n": len(xs),
                    "median": quantile(xs, 0.5),
                    "p25": quantile(xs, 0.25),
                    "p75": quantile(xs, 0.75),
                }
                if xs
                else {"n": 0}
            )
        if samples["real"] and samples["shadow"]:
            row["ks"] = ks_statistic(samples["real"], samples["shadow"])
            row["ratio"] = median_ratio(row["real"]["median"], row["shadow"]["median"])
        else:
            row["ks"] = row["ratio"] = None
        row["verdict"] = (
            verdict(
                row["real"]["n"],
                row["shadow"]["n"],
                row["ks"],
                row["ratio"],
                min_n,
                ks_max,
                ratio_max,
            )
            if row["ratio"] is not None
            else "insufficient"
        )
        rows.append(row)

    agg = dataset.get("aggregate", {})
    rates = {}
    for k in ("real", "shadow"):
        a = agg.get(k, {})
        rates[k] = (
            a["deaths_total"] / a["game_minutes"] if a.get("game_minutes") else None
        )
    dr = {
        "metric": "death_rate",
        "unit": "1/min",
        "level": "kind",
        "real": {
            "n": sum(g["kind"] == "real" for g in dataset["games"]),
            "rate": rates["real"],
        },
        "shadow": {
            "n": sum(g["kind"] == "shadow" for g in dataset["games"]),
            "rate": rates["shadow"],
        },
        "ks": None,
    }
    if rates["real"] is not None and rates["shadow"] is not None:
        dr["ratio"] = median_ratio(rates["real"], rates["shadow"])
        dr["verdict"] = verdict(
            dr["real"]["n"],
            dr["shadow"]["n"],
            None,
            dr["ratio"],
            min_n,
            ks_max,
            ratio_max,
        )
    else:
        dr["ratio"], dr["verdict"] = None, "insufficient"
    rows.insert(1, dr)

    by = {r["metric"]: r["verdict"] for r in rows}
    objectives = {}
    for obj, inputs in OBJECTIVE_INPUTS.items():
        vs = [by.get(m, "insufficient") for m in inputs]
        objectives[obj] = {
            "inputs": inputs,
            # divergent is a result and wins over insufficient
            "realism": "divergent"
            if "divergent" in vs
            else ("similar" if all(v == "similar" for v in vs) else "insufficient"),
        }
    counts = {
        k: Counter(g["mode"] or "?" for g in dataset["games"] if g["kind"] == k)
        for k in ("real", "shadow")
    }
    return {
        "window": dataset["window"],
        "mode": dataset.get("mode", ""),
        "thresholds": {"min_n": min_n, "ks_max": ks_max, "ratio_max": ratio_max},
        "games": {k: dict(v) for k, v in counts.items()},
        "skipped": dataset.get("skipped", {}),
        "metrics": rows,
        "objectives": objectives,
    }


def _fmt(x) -> str:
    if x is None:
        return "-"
    if isinstance(x, float) and math.isinf(x):
        return "inf"
    return f"{x:.3g}"


def render(rep: dict) -> str:
    w = rep["window"]
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    lines = [
        f"Shadow realism, window {iso(w['start'])} .. {iso(w['end'])}"
        + (f", mode {rep['mode']}" if rep["mode"] else "")
    ]
    for k in ("real", "shadow"):
        g = rep["games"].get(k, {})
        lines.append(
            f"  {k}: {sum(g.values())} games "
            + (", ".join(f"{m} {n}" for m, n in sorted(g.items())) or "")
        )
    if rep["skipped"]:
        lines.append(
            "  skipped: "
            + ", ".join(f"{k} {v}" for k, v in sorted(rep["skipped"].items()))
        )
    t = rep["thresholds"]
    lines.append(
        f"  verdict (n = games): similar = n >= {t['min_n']} per kind and enough games that the KS "
        f"critical value (alpha 0.05) is <= {t['ks_max']}, KS <= {t['ks_max']}, "
        f"shadow/real median ratio in [{1 / t['ratio_max']:.2f}, {t['ratio_max']}]; otherwise "
        "insufficient (too few games) or divergent"
    )
    lines.append("")
    lines.append(
        f"{'metric':<24}{'unit':<14}{'real median [p25..p75] n':<30}{'shadow median [p25..p75] n':<30}{'KS':>6}{'ratio':>8}  verdict"
    )
    for r in rep["metrics"]:
        cells = []
        for k in ("real", "shadow"):
            s = r[k]
            if r["level"] == "kind":
                cells.append(f"{_fmt(s.get('rate'))} (aggregate) n={s['n']}")
            elif s["n"]:
                cells.append(
                    f"{_fmt(s['median'])} [{_fmt(s['p25'])}..{_fmt(s['p75'])}] n={s['n']}"
                )
            else:
                cells.append("- n=0")
        lines.append(
            f"{r['metric']:<24}{r['unit']:<14}{cells[0]:<30}{cells[1]:<30}{_fmt(r['ks']):>6}{_fmt(r['ratio']):>8}  {r['verdict']}"
        )
    lines.append("")
    lines.append(
        "Fitness inputs per objective (realism is necessary, not sufficient, for the validity bar, UC-043 A23):"
    )
    for obj, o in rep["objectives"].items():
        lines.append(f"  {obj:<11} {', '.join(o['inputs'])}: {o['realism']}")
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--vm-url", required=True)
    c.add_argument("--jaeger-url")
    c.add_argument("--start", type=float, required=True)
    c.add_argument("--end", type=float, required=True)
    c.add_argument("--mode")
    r = sub.add_parser("report")
    r.add_argument("--dataset", required=True)
    r.add_argument("--min-n", type=int, default=8)
    r.add_argument("--ks-max", type=float, default=0.3)
    r.add_argument("--ratio-max", type=float, default=1.5)
    r.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "collect":
        if a.end <= a.start:
            print("agent-shadow-realism: --end must be after --start", file=sys.stderr)
            return 2
        try:
            ds = collect(VM(a.vm_url), a.start, a.end, jaeger=a.jaeger_url, mode=a.mode)
        except QueryError as err:
            print(f"agent-shadow-realism: {err}", file=sys.stderr)
            return 3
        json.dump(ds, sys.stdout)
        print()
        return 0
    if a.min_n < 1 or a.ks_max <= 0 or a.ratio_max <= 1:
        print(
            "agent-shadow-realism: --min-n >= 1, --ks-max > 0, --ratio-max > 1",
            file=sys.stderr,
        )
        return 2
    with open(a.dataset, encoding="utf-8") as fh:
        rep = compare(json.load(fh), a.min_n, a.ks_max, a.ratio_max)
    print(json.dumps(rep, indent=2, default=str) if a.json else render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
