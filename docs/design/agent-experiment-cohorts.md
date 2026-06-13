# Design: Agent Experiment / Cohort Attribution Framework

**Status:** Design artifact (no implementation) — accompanies epic #982, sub-issues #975–#983.
**Audience:** maintainers and the agent-layer contributors.
**Scope:** how the agent goes from *one shadow experiment, validated against a single batch* to *K concurrent experiments, each a cohort of N games, aggregated and compared to a verdict before promotion*.

---

## TL;DR

The agent can already tune a `game` flag shadow-scoped and observe per-game telemetry, but it has **no first-class notion of an experiment as a cohort of games**. It cannot run several experiments at once, scope a distinct flag override per experiment, aggregate fitness per arm (experimental vs control), or reach a statistical verdict before promotion.

This document designs that layer by **reusing the exact `game_kind` dual-path that already works end-to-end** (eval-context var → telemetry labels/attrs → agent ingest → shadow-scoped JSONLogic targeting → single-run fitness measurement). It adds two attribution dimensions — `experiment_id` and `arm` — that propagate the same way, plus an agent-owned **experiment registry**, a **cohort aggregator** that extends the existing single-run `FitnessMeasurer` (#956), and a **durable experiment journal** (#983) that is the agent's own working memory and keeps the LLM's context bounded as a cohort grows.

The framing follows the M7 self-improvement design: `agent.json` (`:ro` governance, agent never writes), `game.json` (control plane the agent writes shadow-scoped), and now a **third state class** — the agent's own append-only experiment journal.

```
game_kind (shipped)          experiment_id + arm (this design)
  real | shadow         →     exp_<id> + experimental | control
  1 dimension, binary         K experiments × {arm} × N games
```

---

## 1. Problem & motivation

The agent today operates one decision loop per live game (the GameContext Multiplexer + decision LoopSet), can write a shadow-scoped flag experiment to `game.json`, run synthetic shadow games against it, and decide promote/discard/revert from a **single** fitness comparison (the M7-7 Validator). What it cannot do:

- **Run K concurrent experiments.** The shadow/real split is one bit (`game_kind != "real"`). Every shadow game resolves *the* single `agent_experiment` variant on a flag — there is no way to say "these 10 shadow games belong to experiment A on `death_grace_period_seconds`, those 10 to experiment B on `sensitivity`."
- **Scope a flag override per experiment.** `targeting.go` writes one reserved variant name (`ExperimentVariant = "agent_experiment"`) selected by one condition. Two experiments on the same flag would overwrite each other (idempotent-by-design today); two experiments on different flags have no shared experiment identity tying their games together.
- **Carry experimental vs control as an arm.** "Shadow" is monolithic — there is no within-experiment control group to difference against, only a baseline-of-recent-real-games (the M7-7 `Baseline` seam).
- **Aggregate fitness per arm and reach a verdict.** The Validator's `FitnessMeasurer.Measure(run)` returns *one* aggregate number for *one* batch (`services/agent/experiment/validate.go:181`). There is no per-arm sufficient-statistics accumulation, no arm comparison, no sample-size / significance check, no "under-powered → inconclusive."
- **Remember an experiment across restarts or keep the LLM's context bounded** as the cohort grows from 1 game to N.

This framework is what makes self-tuning *measurable*: an experiment becomes a named cohort with arms, fitness aggregates per arm, and a verdict that gates promotion.

---

## 2. Current state (verified, with citations)

Everything below is on `origin/main`. The design extends these; it does not replace them.

### 2.1 The `game_kind` dual-path (the template)

`game_kind` is the proven, end-to-end attribution dimension this design mirrors. Its full path:

1. **Eval-context var, real-by-default.** `GAME_KIND_VAR = "game_kind"`, `GAME_KIND_REAL = "real"`, `GAME_KIND_SHADOW = "shadow"` (`lib/feature_flags.py:62-64`). The API-level context defaults `game_kind` to `"real"` so a context missing it is protected by construction (`lib/feature_flags.py:96-114`). A shadow session overrides it per-session on the contextvars transaction context via `set_game_session_kind_context()` (`lib/feature_flags.py:424-441`) and `set_game_transaction_context(..., game_kind=...)` (`lib/feature_flags.py:444-487`).
2. **Set at the session boundary, before init-frozen reads.** `GameSession._run` calls `set_game_session_kind_context(self._eval_game_kind())` *before* the game mode's `__init__` runs its calibration reads (`services/game_coordinator/game_session.py:175`), because those reads evaluate in the same contextvars context; `_eval_game_kind()` maps the session kind to `real`/`shadow` (`game_session.py:150-158`).
3. **Telemetry emission.** Span attribute `game.kind` on the game span (`game_session.py:187`), alongside `game.id` (`:186`). Metric labels `game_kind=` + `game_id=` on the lifecycle gauges/counters at game end (`game_session.py:251-261`) and at start (`services/game_coordinator/servicer.py:464-467`).
4. **Agent ingest.** The agent reads `game_kind` / `game_id` off the datapoint labels (`attrGameKind`, `attrGameID`, `gameKindOf`, `gameIDOf` in `services/agent/gamecontext/extract_metrics.go:31-70`) and `game.kind` / `game.id` off span attributes; `adoptGame` calls `SetGameKind` / `AdoptSessionID` to enrich the routed partition (`extract_metrics.go:250-261`). `GameContext.GameKind` carries it forward (used by `gamesummary` — `services/agent/gamesummary/summary.go:59`).

### 2.2 Per-game partitioning

The agent already runs **one Store and one decision Loop per game**:

- **GameContext Multiplexer** routes each datapoint/span to a per-`game_id` `Store` partition (`services/agent/gamecontext/multiplexer.go`); `FallbackGameID = ""` is the zero-regression single-game partition (`multiplexer.go:17`). `ApplyMetrics`/`ApplySpans` return the deduped set of touched game_ids (`multiplexer.go:90-150`); lazy create + drained-partition eviction handle lifecycle (`multiplexer.go:214-250`).
- **Decision LoopSet** gives each game its own Loop — its own rate-limit budget, throttle slot, and per-cycle engine state (`services/agent/decision/loopset.go:9-47`), with `Retain` dropping loops in lockstep with evicted partitions (`loopset.go:85-103`).

This is the natural substrate for cohorts: a cohort is a *set of game_ids*, each already an isolated partition.

### 2.3 Binary targeting

The experiment Writer emits exactly one shadow-scoped JSONLogic rule per flag:

```jsonc
{"if": [ {"!=": [{"var":"game_kind"}, "real"]}, "agent_experiment", <existing-else> ]}
```

`buildShadowTargeting` constructs it from raw parts so the pre-existing targeting round-trips byte-for-byte (`services/agent/experiment/targeting.go:54-88`). The condition is **`!= "real"`, not `== "shadow"`**, deliberately: the invariant to protect is "real resolution unchanged," so a missing/unknown `game_kind` resolves the *experimental* variant (shadow-by-default) and only an explicit real game is protected (`targeting.go:18-34`). `ExperimentVariant = "agent_experiment"` is one reserved name → re-experimentation overwrites rather than accumulates (`targeting.go:30-34`).

### 2.4 Single-run measurement (the thing we extend)

The M7-7 Validator owns the *decision logic* and injects three seams (`services/agent/experiment/validate.go`):

- `Baseline.Fitness(ctx, p)` — fitness of recent **real** games (`validate.go:134-138`).
- `SyntheticRunner.Run(ctx, p, games)` → opaque `Run{ID, Games}` (`validate.go:152-170`).
- `FitnessMeasurer.Measure(ctx, run) float64` — **one aggregate number for one batch** (`validate.go:181-185`).

`Validate` computes `delta = after - before` and decides `promote | discard | revert` against a threshold, failing closed on any seam error (`validate.go:267-341`). This is explicitly single-run: one baseline, one batch, one delta.

### 2.5 Promotion gate (reused unchanged)

The M7-8 Promoter takes a validated PROMOTE verdict + `Evidence` and acts per live flags `code_improvement.mode` (`issue|pr|autonomous`) and `.target` (`local|github`) (`services/agent/promote/promote.go:59-124`). Safe defaults (`issue`, `local`), real paths env-gated behind `AGENT_CODE_IMPROVEMENT_ENABLED` + token + kill-switch (`promote.go:17-44`, `:280-312`). `Evidence` already carries `FitnessBefore/After/Delta` + `SyntheticGames` (`promote.go:126-153`).

### 2.6 Compaction precedent (the journal's model)

`gamesummary` distils one game's accumulated `GameContext` (live signals + the #916 rolling timeline) into one compact, self-describing `Summary` — a *derived narrative*, not a re-collection of raw spans (`services/agent/gamesummary/summary.go:1-23`, `BuildSummary` `:209-237`). It is a **pure function** of the snapshot (`:209`). The `Writer` persists it atomically (temp + fsync + rename) one JSON file per game under `/var/lib/joustmania/agent/summaries` (`services/agent/gamesummary/writer.go:24`, `:82-129`), fired once per game on the Store's `OnGameEnd` hook. The journal (§9) is the same idea at *experiment* timescale.

### 2.7 Per-intervention effect analog

`effect.go` (#918) is the per-intervention before/after analog of a cohort verdict: stamp a baseline at dispatch, re-sample after a window, emit the per-objective delta as a span + `agent_intervention_effect_delta` metric (`services/agent/decision/effect.go:20-67`). Note the cardinality discipline already in play — effect deltas are emitted as a *dedicated* metric, not stamped onto per-frame signals.

---

## 3. The model

Three nouns. Keep them small, like `Proposal`.

- **Experiment** — a named, hypothesis-driven unit of measurement. Has an immutable **intent** (goal, hypothesis, the flag + experimental value it tests, target cohort size, target `game_kind`-equivalent), a **lifecycle status**, and accumulates a journal. Identified by `experiment_id` (e.g. `exp_<uuid hex[:12]>`, mirroring the `game_<uuid hex[:12]>` shape).
- **Arm** — a treatment within an experiment. The minimal set is two:
  - **experimental** — games that resolve the experiment's flag override.
  - **control** — games that resolve the flag's existing default (no override), but are otherwise drawn from the same population and tagged with the experiment so their fitness aggregates alongside the experimental arm. This gives a *within-experiment* difference, complementing (not replacing) the M7-7 recent-real baseline.

  `arm ∈ {experimental, control}` initially; the model leaves room for >2 arms (multi-value sweeps) without schema change.
- **Cohort** — the set of game_ids assigned to a given `(experiment_id, arm)`. Concretely, a cohort is "which partitions in the Multiplexer belong to this arm." Fitness is aggregated per cohort.

All experiment games are **shadow** games in the `game_kind` sense — the framework never touches real players. `experiment_id`/`arm` are *finer-grained labels within shadow*, never a replacement for the real/shadow safety bit.

---

## 4. Identity propagation (#975)

Mirror `game_kind` exactly, with two new dimensions.

### 4.1 Eval-context vars

Add `experiment_id` and `arm` to the transaction-level evaluation context, set on a shadow session that belongs to an experiment — alongside the existing `game_kind`. The natural extension point is `set_game_transaction_context(...)` / a sibling `set_experiment_context(...)` in `lib/feature_flags.py` (next to `set_game_session_kind_context`, `feature_flags.py:424`). **Real-by-default carries over for free:** the API-level context has no `experiment_id`, so a non-experiment game's experiment targeting condition is false (see §6) — exactly how a missing `game_kind` defaults to protected.

Proposed constants (next to `GAME_KIND_VAR`, `feature_flags.py:62`):

```
EXPERIMENT_ID_VAR = "experiment_id"   # absent ⇒ not in any experiment
ARM_VAR           = "arm"             # "experimental" | "control"
ARM_EXPERIMENTAL  = "experimental"
ARM_CONTROL       = "control"
```

### 4.2 Telemetry

- **Spans:** add `experiment.id` and `experiment.arm` attributes to the game span next to `game.kind` / `game.id` (`game_session.py:186-187`). Spans are the **primary attribution channel** — unbounded-cardinality attributes are fine on spans (each is one trace), and the agent already ingests game identity off spans.
- **Metrics — cardinality decision (load-bearing):** `experiment_id` is high-cardinality (one new value per experiment, indefinitely). It MUST NOT be added as a label to high-volume per-frame metrics (`game_player_accel_magnitude`, `game_player_alive`, etc., enumerated in `extract_metrics.go:13-23`) — that multiplies every series by the number of live experiments and blows up the TSDB. Instead:
  1. **Span attribution** carries `experiment_id`/`arm` for per-game/per-player detail (no cardinality cost in the TSDB).
  2. A **dedicated low-rate experiment metric** — emitted once per game-end or per aggregation tick, not per frame — carries `experiment_id` + `arm` labels (e.g. `agent_experiment_game_fitness` as a gauge/histogram at game-end). This is the same discipline as `agent_intervention_effect_delta` (`effect.go:308-312`): a *separate* low-rate metric rather than stamping the dimension onto the firehose.

### 4.3 Agent ingest

Extend `gameLabels` (`extract_metrics.go:51-54`) with `ExperimentID` / `Arm`, resolved by `experimentIDOf` / `armOf` helpers mirroring `gameIDOf`/`gameKindOf` (`:56-70`), and store them on `GameContext` next to `GameKind` (so `gamesummary` / the aggregator can read them). `adoptGame` (`:258-261`) gains `SetExperimentID` / `SetArm` setters that no-op on empty — an unlabeled signal leaves the store unchanged, exactly as today.

**Routing is unchanged:** the Multiplexer still partitions on `game_id` (`multiplexer.go:117`). `experiment_id`/`arm` are *enrichment within a partition*, not a routing key — a cohort is reconstructed by grouping partitions that share an `experiment_id` (§8), not by adding a third partition map.

---

## 5. Spawn binding (#976)

When the agent (or the M6 shadow-game harness) starts a shadow game for an experiment, it binds that game to `(experiment_id, arm)` **at spawn**, threaded through the same `set_game_transaction_context` / `set_game_session_kind_context` boundary that already establishes `game_kind` before init-frozen reads (`game_session.py:175`). The experiment context must be in place at the same point, for the same reason: a flag whose override is read at `__init__` would otherwise miss its experiment.

**Ground-truth-at-spawn, not `hash(game_id)`.** Two ways to decide which arm a game is in:

- *Hash:* derive arm from `hash(game_id) % 2`. Tempting (stateless, no registry write) but **rejected**: it couples arm assignment to the opaque id format, makes control/experimental ratios drift as ids are minted, can't express unequal split or >2 arms, and gives the agent no record of *why* a game is in an arm.
- *Ground-truth (chosen):* the agent decides the arm when it spawns the game and records `(game_id → experiment_id, arm)` in the registry/journal at that instant. The eval context is set from that decision. **No drift:** the assignment is authoritative and immutable for that game's lifetime; telemetry merely *reports* it. This matches the rest of the system — the agent already owns spawn decisions, and the journal (§9) needs the assignment recorded as an event anyway.

The spawn binding writes one journal `game_assigned` event (§9) at the moment of assignment.

---

## 6. Experiment-scoped targeting (#977)

Extend `targeting.go` from one binary condition to **experiment-keyed JSONLogic**. For a flag under experiment `exp_X` whose experimental arm should resolve value `V`:

```jsonc
{"if": [
  { "and": [ {"==": [{"var":"experiment_id"}, "exp_X"]},
             {"==": [{"var":"arm"}, "experimental"]} ] },
  "agent_experiment__exp_X",          // experiment-scoped variant name
  <existing-else>                      // control + real + everything else falls through
]}
```

Key properties, all extensions of existing `targeting.go` behavior:

- **Control arm resolves the existing default by construction.** A `control` game has `experiment_id = exp_X` but `arm = control`, so the `and` is false and it falls through to `<existing-else>` — i.e. the flag's pre-experiment value. No separate control variant needed; control *is* the else-branch. This is the same trick as the current `!= real` else-fallthrough (`targeting.go:40-53`).
- **Multi-experiment coexistence.** K experiments on K *different* flags are K independent flag rules — no interaction. K experiments on the *same* flag nest as chained `if`s (one experiment's else-branch is the next experiment's `if`), each keyed on its own `experiment_id`. The reserved variant name becomes `agent_experiment__<experiment_id>` so experiments never overwrite each other (replacing the single `ExperimentVariant`, `targeting.go:34`).
- **Real-by-default fail-safe preserved.** A real game has no `experiment_id` (API-level default omits it), so *every* experiment's condition is false and it resolves `<existing-else>` → the real default. The Gate's invariant ("real resolution unchanged," `proposal.go:22-27`) holds by the same structural argument as today; the Gate is extended to verify it across *all* experiment branches, not just one.

The Writer's atomic read-modify-write of the flagd file under a mutex (the `actions/writer.go` pattern) is reused unchanged — only the rule shape grows.

---

## 7. Registry & lifecycle (#978)

An **agent-owned coordinator** holds the live experiment set. It is the experiment-timescale counterpart to the per-game LoopSet/Multiplexer: where those manage one loop/store per game, the registry manages one entry per experiment.

### 7.1 Responsibilities

- Mint `experiment_id`, hold each experiment's intent (§9a) and status.
- **Capacity allocation across concurrent experiments.** Shadow-game capacity is finite (the coordinator's concurrency slots — real games preempt shadow, `servicer.py:452-463`). The registry decides how many in-flight shadow games each experiment may hold so K experiments share capacity fairly rather than one starving the others. (Exact fairness policy is an open question — §14.)
- Drive arm assignment on spawn (§5) and own the `(game_id → experiment_id, arm)` map.
- Trigger aggregation (§8) as games conclude and transition status on the verdict.

### 7.2 Status machine

```
PROPOSED ─▶ RUNNING ─▶ CONCLUDED ─▶ {PROMOTING ─▶ DONE | DISCARDED}
                 │
                 └─▶ ABORTED   (capacity reclaimed / kill-switch / error)
```

- **PROPOSED** — intent recorded; no games yet.
- **RUNNING** — accruing games across arms; aggregation runs on each game-end.
- **CONCLUDED** — target N reached (or under-powered timeout); verdict computed.
- **PROMOTING / DISCARDED** — verdict routed to the Promoter (§10) or dropped.
- **DONE / ABORTED** — terminal; capacity released; targeting rule torn down.

The registry survives restart by rehydrating from the durable journal (§9) — the in-memory registry is a *view* of the journal, not the source of truth.

---

## 8. Cohort aggregation & verdict (#979)

This is the extension of the M7-7 single-run `FitnessMeasurer` from *one batch → one number* to *a cohort → per-arm aggregates → a verdict*.

### 8.1 Grouping

The agent already has per-game fitness (the `EvaluateFitness` the decision loop computes, and the per-game `Summary`). To aggregate a cohort, **group the per-game GameContexts/summaries by `(experiment_id, arm)`** using the labels propagated in §4. Concretely: as each experiment game ends (the `OnGameEnd` hook that already fires `gamesummary`), the aggregator folds that game's fitness sample into the matching `(experiment_id, arm)` accumulator.

### 8.2 Sufficient statistics (online, bounded)

Per arm, keep **Welford running statistics** — `count`, `mean`, `M2` (→ variance) — updated incrementally per game-end. This is O(1) memory per arm regardless of N (critical for the journal's bounded-context property, §9c). No raw per-game fitness list is retained in the live aggregate (it lives in the append-only log on disk, read only offline).

### 8.3 Arm comparison & verdict

- **Difference of means** between `experimental` and `control` arms (plus, where useful, the M7-7 recent-real baseline as a sanity anchor).
- **Sample-size / significance:** the verdict is only `PROMOTE`/`DISCARD` when each arm has cleared a minimum count *and* the difference clears a significance bar; otherwise the verdict is **`INCONCLUSIVE`** (under-powered). The exact test (Welch's t-test on the running stats is the leading candidate, since variance is tracked; a simpler effect-size + min-N gate is the conservative fallback) is **deferred — §14**. The interface is designed so the test is swappable.
- Extends, not replaces, `validate.go`: the new `CohortMeasurer` produces a `CohortVerdict{Outcome, ExperimentalMean, ControlMean, Delta, NExperimental, NControl, Significant}` that maps onto the existing `experiment.Decision` (`validate.go:76-95`) so the Promoter consumes it unchanged. `OutcomePromote/Discard` carry over; a new `OutcomeInconclusive` covers under-powered.

```
M7-7 single-run            this design (cohort)
─────────────────          ───────────────────
Baseline.Fitness()    →    + control-arm running mean (within-experiment)
Measure(run) → float  →    CohortMeasurer: per-arm Welford → verdict
delta vs threshold    →    arm difference vs significance + min-N
                           under-powered ⇒ INCONCLUSIVE (new)
```

---

## 9. Experiment journal & historical progression (#983)

**First-class.** The journal is how the agent remembers an experiment over its lifetime *and* how the LLM's context stays bounded as the cohort grows. It is the **third state class** in the M7 model: distinct from `agent.json` (`:ro` governance the agent only reads) and `game.json` (the control plane the agent writes), the journal is the **agent's own working memory** — the agent both writes and reads it, and nothing else governs it.

### 9.1 Three-part record

Every experiment's journal has three parts with different mutability:

**(a) INTENT — immutable.** Written once at PROPOSED. The goal/hypothesis + the full experiment config. Never rewritten.

**(b) EVENT LOG — append-only.** One entry per material event, in order, **never rewritten**:
`game_assigned` (spawn binding, §5) · `game_concluded` (+ that game's fitness sample) · `interim_verdict` (the rolling verdict after each conclusion) · `decision` (status transition) · `outcome` (terminal). The log is the audit trail and the offline replay source. It is the only place raw per-game fitness samples are retained.

**(c) ROLLING SUFFICIENT-STATISTICS SUMMARY — rewritten in place.** Per-arm `count / mean / variance` via Welford (§8.2) + the current verdict. O(1) size in N. This is the compact view the live registry and the LLM read.

This is exactly the `gamewindow`/`gamesummary` compaction (§2.6) lifted to *experiment* timescale: a stream of game-ends distilled into a constant-size summary.

### 9.2 Bounded-LLM-context discipline (load-bearing)

When the agent asks an LLM to reason about an experiment (e.g. the #844 post-game retro, or a "should I conclude this?" prompt), the prompt contains **intent + the rolling aggregates (c) + a short recent tail of the event log** — *never* the raw game-by-game log. Therefore prompt size stays roughly **constant as N grows**: 50 games and 500 games produce the same-sized prompt. This is the same reason `gamesummary` derives a compact narrative instead of replaying spans (`summary.go:1-23`). It feeds the #844 retro path directly.

### 9.3 Durability

The journal is **authoritative on a local mounted volume** (JSON or SQLite), surviving agent restart. This is a deliberate fix for the in-memory-loss failure mode (#831): the live registry is rebuilt *from* the journal on startup; telemetry (spans/metrics) is a **view**, never the source of truth. Reuse the proven `gamesummary` persistence pattern — atomic temp + fsync + rename, env-overridable dir (`writer.go:24`, `:82-129`):

- Layout option A (JSON, mirrors `gamesummary`): one dir per experiment, `intent.json` (immutable) + `events.jsonl` (append-only) + `summary.json` (rewritten atomically). Append-only JSONL is the natural shape for (b); atomic rename for (c).
- Layout option B (SQLite): one DB, tables `experiment` / `event` / `arm_stat`. Better for queries/concurrency; heavier dependency. **Open question — §14.**

Default dir e.g. `/var/lib/joustmania/agent/experiments` (env `AGENT_EXPERIMENT_DIR`), mirroring `AGENT_GAME_SUMMARY_DIR` (`writer.go:27`).

### 9.4 Example schema

**(a) intent record** (`intent.json`):
```jsonc
{
  "schema_version": 1,
  "experiment_id": "exp_a1b2c3d4e5f6",
  "created_at": "2026-06-13T10:00:00Z",
  "hypothesis": "Raising death_grace_period to 500ms reduces frustration deaths without lengthening games.",
  "flag_key": "death_grace_period_seconds",
  "experimental_value": 0.5,
  "objective": "engagement_balanced",
  "target_n_per_arm": 20,
  "arms": ["experimental", "control"]
}
```

**(b) event-log entries** (`events.jsonl`, one JSON object per line):
```jsonc
{"seq":1,"at":"2026-06-13T10:01:12Z","kind":"game_assigned","game_id":"game_0f1e2d3c4b5a","arm":"experimental"}
{"seq":2,"at":"2026-06-13T10:03:41Z","kind":"game_concluded","game_id":"game_0f1e2d3c4b5a","arm":"experimental","fitness":0.71,"duration_s":148.2}
{"seq":3,"at":"2026-06-13T10:03:41Z","kind":"interim_verdict","outcome":"inconclusive","n_experimental":1,"n_control":0}
```

**(c) rolling-summary snapshot** (`summary.json`, rewritten atomically each conclusion):
```jsonc
{
  "schema_version": 1,
  "experiment_id": "exp_a1b2c3d4e5f6",
  "status": "running",
  "updated_at": "2026-06-13T11:42:00Z",
  "arms": {
    "experimental": {"count": 18, "mean_fitness": 0.704, "variance": 0.0121},
    "control":      {"count": 17, "mean_fitness": 0.661, "variance": 0.0144}
  },
  "verdict": {"outcome": "inconclusive", "delta": 0.043, "significant": false,
              "reason": "n_per_arm < target (20)"}
}
```

---

## 10. Promotion & teardown (#980)

On a `CONCLUDED` experiment with a `PROMOTE` verdict, route to the **existing Promoter unchanged** (`promote.go`, §2.5): build `Evidence` from the cohort verdict — `FitnessBefore = control mean` (or recent-real baseline), `FitnessAfter = experimental mean`, `FitnessDelta`, `SyntheticGames = N` (`promote.go:130-153`) — and call `Promote(...)`. Safe defaults and env-gated real paths apply as-is; nothing in the promotion safety rail changes.

**Teardown** is the inverse of §6: on any terminal status (`DONE`/`DISCARDED`/`ABORTED`), the Writer removes that experiment's `if`-branch from the flag rule (un-nesting its `experiment_id` condition), releasing its variant name and reclaiming its capacity (§7). Removing a shadow-scoped branch only ever affects shadow games, so it is trivially invariant-safe (same argument as `validate.go`'s Reverter note, `:194-199`). Auto-stop also fires on the kill-switch (`agent.json` `enabled`).

---

## 11. Dashboard (#981, M5)

An **experiments view** in the M5 dashboard, sourced from the dedicated low-rate experiment metric (§4.2) + the journal summaries + experiment spans. Per experiment: status, per-arm count/mean/variance, current verdict, and an experimental-vs-control fitness comparison over time. This consumes the same telemetry the framework emits — no new producer. It is the cohort-level sibling of the #918 intervention-effect panels and the #933 game-summary pane.

---

## 12. Key design decisions

| Fork | Options | Chosen | Rationale |
|------|---------|--------|-----------|
| Arm assignment | hash(game_id) vs ground-truth-at-spawn | **ground-truth** | no drift; authoritative; supports unequal/>2 arms; recorded in journal anyway (§5) |
| Control type | recent-real baseline only vs within-experiment control arm | **both** (control arm primary, baseline as anchor) | within-experiment control controls for population/time confounds the baseline can't (§3, §8.3) |
| Statistics | last-value vs running Welford | **Welford** | O(1) memory in N → keeps the journal & LLM context bounded (§8.2, §9) |
| Cardinality | `experiment_id` on per-frame metrics vs span + dedicated metric | **span attribution + 1 low-rate metric** | avoids TSDB blow-up; mirrors `agent_intervention_effect_delta` (§4.2) |
| Targeting | overwrite single variant vs experiment-keyed nested `if` | **nested `if`, `experiment_id`-keyed** | K experiments coexist without clobbering; control = else-branch (§6) |
| Source of truth | in-memory registry vs durable journal | **durable journal; registry is a view** | survives restart; fixes #831 in-memory loss (§9.3) |
| Routing | add `experiment_id` partition map vs reuse `game_id` partitions | **reuse `game_id`; group by `experiment_id`** | partitioning already correct; cohort = grouping, not a third map (§4.3) |

---

## 13. Phased plan

```
#975 identity propagation (eval-context + telemetry + ingest)   [foundational]
        │
        ├──▶ #976 spawn binding (assign arm at spawn)
        │
        └──▶ #977 experiment-scoped targeting (nested if, Gate)
                        │
                        ▼
              #978 registry & lifecycle  ◀───┐
                        │                    │ co-developed
                        │              #983 journal (durable, 3-part)  [foundational w/ #978]
                        ▼
              #979 cohort aggregation & verdict (Welford → significance)
                        │
                        ▼
              #980 promotion (reuse Promoter) & teardown
                        │
                        ▼
              #981 dashboard experiments view            [M5]
```

- **#975 first** — nothing attributes without it (the dual-path template).
- **#976 + #977** depend on #975 and are parallelizable.
- **#978 registry** needs targeting (#977) to allocate experiments to flags; **#983 journal is foundational *with* #978** — the registry is a view of the journal, so they co-develop.
- **#979** needs identity (#975) + a live registry (#978) to group cohorts.
- **#980** reuses the existing Promoter; small.
- **#981** is M5, after the telemetry of #975/#979 exists.

---

## 14. Open questions / risks

Honest unknowns that want a human decision before implementation:

1. **Significance test.** Welch's t-test on the running stats, a non-parametric test, or a conservative min-N + effect-size gate? Fitness distributions are not known to be normal, and N per arm will often be small (≤20). The verdict interface is swappable, but the *default* needs a maintainer call. (§8.3)
2. **Shadow capacity provisioning.** How many concurrent shadow games can the box actually sustain alongside real play, and how is that budget surfaced to the registry? Real games preempt shadow today (`servicer.py:452-463`); the registry needs a *number* to allocate against. (§7.1)
3. **Multi-experiment scheduling fairness.** When K experiments compete for limited shadow slots, what's the policy — round-robin, weighted by under-poweredness, FIFO by PROPOSED time? Risk: one slow experiment starves the rest. (§7.1)
4. **Journal storage backend.** JSONL+JSON (matches `gamesummary`, zero new deps) vs SQLite (better queries/concurrency, heavier). Leaning JSONL for parity; SQLite if query needs grow. (§9.3)
5. **Control-arm cost.** A within-experiment control doubles shadow-game spend per experiment. Is the confound-control worth the capacity, or is the recent-real baseline (free) sufficient for early experiments? Possibly make the control arm opt-in per experiment.
6. **Confounding across concurrent experiments on the same flag.** Nested `if` branches are mutually exclusive per game, but two experiments on *related* flags running simultaneously could interact. The framework attributes per `(experiment_id, arm)` honestly, but cross-experiment interaction is not modeled — flagged, not solved.
7. **`game_kind` interaction with arms.** Confirm control games are still `game_kind = "shadow"` (they are synthetic), so the real-protection invariant is untouched and `experiment_id`/`arm` only ever subdivide *shadow*.

---

## References

- M7 self-improvement design (rescoped 2026-06-12): flag experiments, not source edits; `agent.json` `:ro` governance vs `game.json` control plane vs (now) the experiment journal as the agent's working memory.
- `docs/research/844-post-game-retro.md` — the between-games LLM path the bounded journal feeds.
- `docs/research/722-intervention-surface.md` — the signal surface fitness is computed from.
- `docs/research/774-shadow-games.md` — the M6 shadow-game harness experiments run on.
- Current code: `lib/feature_flags.py`, `services/game_coordinator/{game_session,servicer}.py`, `services/agent/gamecontext/{extract_metrics,multiplexer}.go`, `services/agent/decision/{loopset,effect}.go`, `services/agent/experiment/{targeting,proposal,validate}.go`, `services/agent/gamesummary/{summary,writer}.go`, `services/agent/promote/promote.go`.
