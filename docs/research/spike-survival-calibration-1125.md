# Spike-survival metric calibration (#1125, unblocked by #1145)

**Status:** offline calibration evidence. The live fitness path (`services/agent/decision/fitness.go`)
is **unchanged** — `spikeSurvivalRatio` stays observability-only until #1125 consumes this.

## TL;DR

**Methodology + pipeline VALIDATED; discriminator separation NOT yet established.**
The query helper, feature computation, window discovery, and sanity-check
machinery all work end-to-end against the live stack. But the headline "CV
separates erratic from steady" claim is **not yet evidence**: on the unlabeled
mock data currently available, the cohorts are defined BY CV quartiles and then
scored on CV, so the ≈1.7 "separation" is **circular / self-fulfilling**, not a
discriminator result. The absolute `k` and the discriminator choice must be
**re-derived on ELIMINATION-LABELED real-hardware games** (the #1017 data path)
before #1125 binds anything to gating.

- The per-player, per-game acceleration series **already exists** in the live
  Prometheus/VM stack as `game_player_accel_magnitude{game_id, serial}` at the
  native ~0.5 s otel push resolution, covering full game windows. No new
  instrumentation is needed to calibrate #1125 — only an **offline** query.
  (Pipeline validated.)
- **SOUND finding (independent of the circularity):** the legacy `std/peak <= 0.30`
  check (#1120) does **not** bite — the recorded distribution sits an order of
  magnitude below 0.30 (p75 = 0, max ≈ 0.12) and barely moves between players.
  This directly validates the #1120 scale-invariance failure mode and does not
  depend on any CV cohorting.
- **SOUND finding (independent of the circularity):** with an **absolute std
  floor** defining "steady" (NOT a CV split), **0 / 1739 steady players are
  flagged** at the candidate bound. The "spares-steady" property is real because
  the steady cohort is defined by an absolute floor, not by the discriminator
  under test.
- **NOT yet established:** that CV (or any feature) actually *separates* erratic
  from steady movers. The ≈1.2–1.8 "separation" was measured against cohorts
  that were themselves cut from CV quartiles — a reviewer reproduced ≈1.81 on a
  smooth, featureless smear with no real structure. This is a property of the
  circular labeling, not of the data.
- **Provisional only:** `k ≈ 0.015` on CV is the survivor p75 *of a circular
  split*. Treat it as a placeholder shape for the eventual real-data derivation,
  **not** a defensible bound.
- **Must read before binding:** the data currently available is predominantly
  **mock-substrate** (synthetic uniform movement), exactly the substrate #1125
  warns cannot finalize this calibration. Re-derive the discriminator choice AND
  the absolute `k` on real-hardware, elimination-labeled games (#1017) before
  #1125 binds it into fitness. Deliverable here: **pipeline + methodology
  validated, k provisional.**

## Endpoint / retention finding

- Working Prometheus endpoint: **`http://localhost/prometheus/`** (HTTP 200).
  `http://localhost:8080/prometheus/` did **not** respond (000) in this stack.
- `joustmania-victoria-metrics`: `-retentionPeriod=7d`.
- `joustmania-prometheus`: `--storage.tsdb.retention.time=30d`. This is the
  endpoint `localhost/prometheus` serves, and it scrapes the otel-collector push
  pipeline (`pipeline="otel-push"`, job `game-coordinator-service`) which carries
  `game_player_accel_magnitude` at a **~0.5 s native sample interval** (≈3925
  samples over a ~2070 s game). **Retention and resolution are sufficient to
  calibrate full games — no downsampling recording rule is required.**
- Note: `controller_accel_magnitude{serial}` (the raw per-controller series named
  in #1145) also exists, but its `prometheus-pull` copy is only 10 s resolution and
  is **not** game-scoped. `game_player_accel_magnitude{game_id, serial}` is the
  better source: game-scoped, per-player, 0.5 s native — and inherently
  session-scoped, honoring no-player-identity (#23).

## Trajectory features (per `(game_id, serial)`)

Computed by `scripts/calibration/vm_trajectory_features.py` via PromQL
`query_range` over a game's `[start, end]` window:

| feature | definition |
|---|---|
| `peak` | max magnitude (g) |
| `mean` | mean magnitude (g) |
| `std` | population std (g) |
| `cv` | `std / mean` — scale-free spike signal |
| `std_peak` | `std / peak` — legacy #1120 ratio, kept for comparison |
| `spike_count` | upward crossings of `mean + k·std` (discrete bursts) |
| `spike_rate_per_min` | `spike_count` per minute |
| `slope_g_per_s` | least-squares trend of magnitude vs. time |

Game windows are discovered automatically from the same series
(`discover_game_windows`); survivor vs. eliminated is read from
`game_player_elimination_order{game_id, serial}` when present (else `eliminated=None`).

## Evidence (latest run, `localhost/prometheus`, 48 h lookback)

- 467 game windows (mostly 300 s shadow games), 1868 player records, **129 movers**
  (players with std ≥ 0.01 g; the other ~1739 are flat synthetic/idle controllers).

- **CIRCULAR — do NOT read as discriminator evidence.** Because the mock games
  carry no eliminations, the script falls back to an **internal CV-quartile split**:
  it cuts the "survivor" and "erratic" cohorts BY CV quartile and then scores CV
  against those very cohorts. Any feature correlated with CV will look like it
  "separates" — a reviewer reproduced ≈1.81 separation from a smooth, featureless
  smear. The numbers below are reported for transparency, not as proof CV
  discriminates:

| discriminator | survivor p75 | erratic median | "separation" (self-fulfilling) |
|---|---|---|---|
| cv | 0.0126 | 0.0745 | +1.76 (circular: cohorts cut from CV quartiles) |
| spike_rate_per_min | 0.197 | 0.000 | −0.32 |

- **SOUND — independent of the circularity. Sanity check at k = 0.0126 (CV):**
  - steady players flagged: **0 / 1739** (flag rate 0.0) → *spares steady*.
    "Steady" here is defined by an **absolute std floor** (`std < 0.01 g`), NOT by
    CV, so this 0/1739 result is genuine: at this bound the metric does not fire on
    near-stationary controllers. This is the one survivor-side property that holds
    regardless of the cohorting.
  - top-quartile movers flagged: 32 / 32 → reported, but "erratic" here is the
    CV-defined top quartile, so this side is circular and is NOT evidence the
    metric catches genuinely erratic play.
- Resolution note: re-running at step 0.5 s vs 1.0 s gives k = 0.0152 vs 0.0137.
  The *value* is stable across resolution, but stability of a circular estimate
  does not make it a discriminator bound — it stays provisional.
- **SOUND — independent of the circularity.** Legacy `std/peak` distribution
  (p75 = 0.0, max = 0.119) sits an order of magnitude below the 0.30 bound across
  the whole population — direct confirmation of the #1120 "ratio never bites"
  failure mode. This needs no CV cohorting and stands on its own.

## Candidate metric for #1125 (provisional — not yet a recommendation)

The legacy `std/peak` ratio is ruled OUT (it never bites — sound finding above).
CV (`std / mean`) is the **leading candidate** to replace it, because it is
non-scale-invariant (a flag that scales movement intensity changes mean and std
together but reshapes burstiness, moving CV) — so unlike `std/peak` it *can* in
principle produce arm-to-arm separation.

But **#1125 must not bind CV (or any feature) yet.** This doc does **not**
establish that CV separates erratic from steady movers: the ≈1.7 separation came
from a CV-quartile split scored on CV (circular, see Evidence). Before #1125 picks
a discriminator and a bound:

1. Re-run on **elimination-labeled real-hardware games** (#1017) so cohorts are
   defined by actual outcomes, not by the feature under test.
2. Re-derive the discriminator choice (CV vs spike-rate vs others) from that
   real, labeled separation.
3. Re-derive the absolute bound `k` from the real survivor/eliminated gap. The
   provisional `k ≈ 0.015` shown here is a placeholder shape, not a defensible
   value.

Until then the deliverable is: **pipeline + methodology validated, k provisional.**

## How to reproduce

```bash
cd scripts/calibration
uv run python calibrate_spike_survival.py --lookback-hours 48 --min-duration-s 60 --json report.json
```

The helper and calibration script are **offline-only**: stdlib HTTP, no new runtime
dependency, never imported by the live agent loop or fitness path (Pi-safe). They
degrade clearly (non-zero exit + explicit message) when the stack is unreachable or
no full game is recorded.
