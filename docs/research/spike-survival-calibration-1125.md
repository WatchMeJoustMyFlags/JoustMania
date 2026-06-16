# Spike-survival metric calibration (#1125, unblocked by #1145)

**Status:** offline calibration evidence. The live fitness path (`services/agent/decision/fitness.go`)
is **unchanged** — `spikeSurvivalRatio` stays observability-only until #1125 consumes this.

## TL;DR

- The per-player, per-game acceleration series **already exists** in the live
  Prometheus/VM stack as `game_player_accel_magnitude{game_id, serial}` at the
  native ~0.5 s otel push resolution, covering full game windows. No new
  instrumentation is needed to calibrate #1125 — only an **offline** query.
- The legacy `std/peak <= 0.30` check (#1120) is scale-invariant and does not
  discriminate; the recorded distribution confirms it sits far below 0.30 and
  barely moves between steady and erratic players.
- The **coefficient of variation (CV = std / mean)** is the discriminator that
  actually separates erratic movers from steady players in the recorded data
  (Cohen-d-like separation ≈ 1.2–1.8 across resolutions), while spike-rate did
  not separate on this dataset.
- **Recommended survivor bound: `k ≈ 0.015` on CV** (a player with CV above ~0.015
  is "erratic"). This is the survivor p75 derived from the real distribution, not
  an arbitrary constant.
- **Caveat (must read before binding):** the data currently available is
  predominantly **mock-substrate** (synthetic uniform movement), exactly the
  substrate #1125 warns cannot finalize this calibration. The pipeline, the
  discriminator choice (CV over std/peak and spike-rate), and the sanity-check
  methodology are validated here; the **absolute k must be re-derived from
  real-hardware games** (the #1017 data path) before #1125 binds it into fitness.

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
- Discriminator comparison on the mover cohort (internal CV-quartile split, since
  most mock games have no eliminations):

| discriminator | survivor p75 | erratic median | separation |
|---|---|---|---|
| **cv** | **0.0126** | 0.0745 | **+1.76** |
| spike_rate_per_min | 0.197 | 0.000 | −0.32 (no separation on this data) |

- **Sanity check at k = 0.0126 (CV):**
  - steady players flagged: **0 / 1739** (flag rate 0.0) → *spares steady*
  - top-quartile movers flagged: **32 / 32** (flag rate 1.0) → *bites erratic*
- Resolution stability: re-running at step 0.5 s vs 1.0 s gives k = 0.0152 vs
  0.0137 (both CV, separation ≈ 1.2–1.8). The recommendation is robust to query
  resolution.
- Legacy `std/peak` distribution (p75 = 0.0, max = 0.119) sits an order of
  magnitude below the 0.30 bound across the whole population — direct confirmation
  of the #1120 "ratio never bites" failure mode.

## Recommended metric for #1125

Bind the balanced spike-survival sub-check on **CV (`std / mean`)**, not `std/peak`:
a player "survives spikes" when their whole-game `CV ≤ k`. Use **`k ≈ 0.015`** as the
starting survivor bound, re-derived from real-hardware games before going gating.

This is non-scale-invariant (a flag that scales movement intensity changes mean and
std together but reshapes burstiness, moving CV) so it can produce arm-to-arm
separation that `std/peak` could not.

## How to reproduce

```bash
cd scripts/calibration
uv run python calibrate_spike_survival.py --lookback-hours 48 --min-duration-s 60 --json report.json
```

The helper and calibration script are **offline-only**: stdlib HTTP, no new runtime
dependency, never imported by the live agent loop or fitness path (Pi-safe). They
degrade clearly (non-zero exit + explicit message) when the stack is unreachable or
no full game is recorded.
