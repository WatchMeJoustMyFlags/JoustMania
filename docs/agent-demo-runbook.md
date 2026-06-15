# Agent Demo Runbook — setup, failure modes, recovery

The operator playbook for the **live stage demo** of the agentic control stack:
the experiment cohort loop declaring + concluding experiments, the agent making
decisions (rules / LLM), live interventions, the dashboards, and live flag flips
via flagd.

This runbook is the **glue** between the focused runbooks — it does not duplicate
them. Read the linked one when you need the detail:

- [`agent-dry-run-runbook.md`](agent-dry-run-runbook.md) — the experiment cohort
  loop (`make dry-run`, the enable script, the gating chain). **The core of the
  demo.**
- [`agent-act-runbook.md`](agent-act-runbook.md) — the live-intervention ACT path
  (two gates, the kill-switch panic button, blocked vs applied interventions).
- [`agent-ollama-host-runbook.md`](agent-ollama-host-runbook.md) — reaching a
  **host** Ollama for real inference (the `0.0.0.0` + WSL-gateway foot-guns).
- [`agent-inference-gateway.md`](agent-inference-gateway.md) — the optional
  LiteLLM gateway (provider routing + cloud fallback).

> **Do a full dry-rehearsal of every step in this runbook on the demo hardware
> before the talk.** Several steps (model cold-start, host-Ollama networking,
> controller pairing) are environment-specific and only fail on stage.

---

## 1. Setup / bring-up

### Prerequisites

- **Docker + Docker Compose** on the demo host. That alone runs the entire
  stack with mock controllers and the **stub** inference backend (decisions on
  `mode=rules`) — no LLM, no hardware required. This is the safe fallback the
  whole demo can run on.
- *(Optional, for real inference)* an **Ollama** reachable from the agent
  container with a model pulled (`ollama pull phi4-mini`). Reaching a *host*
  Ollama is the #1 live foot-gun — see step *Point at real inference* and the
  [host-Ollama runbook](agent-ollama-host-runbook.md).
- *(Optional, real PS Move controllers)* a Bluetooth adapter; the demo runs on
  **mock controllers** by default (the `make dry-run` / `up-mock` flagd `ci/`
  dir sets controller `backend=mock`), so hardware is not on the critical path.

### Bring up the demo stack (the cohort loop)

The demo runs on the **dry-run stack** — latest images, mock controllers, full
observability, the agent + dashboard profiles, and one seeded experiment. From
the repo root:

```bash
make dry-run
```

The loop is **inert until you opt in** (both live gates are fail-closed `off` by
default). Flip them on — the gate flips take effect **live** (~1 s; since #1044
the loop is always built and self-gates each tick on the live flags, so an
off→on flip starts it with **no agent restart**):

```bash
./scripts/agent-dryrun-enable.sh on
```

`agent-dryrun-enable.sh on` flips, **in the `services/flagd/ci/` flag dir only**
(never the production defaults), three flags in one step:

- `enabled` → `on` — the master kill-switch.
- `experiments_enabled` → `on` — the LIVE experiment-loop gate (post-#1044; this
  flag **overrides** the `AGENT_EXPERIMENTS_ENABLED` env, which is dead config
  for this purpose).
- `mode` → `llm` — **only when `AGENT_INFERENCE_BACKEND=openai` is set** (see
  next section); otherwise `mode` is left on `rules` (correct for the stub).

`make dry-run` prints these exact steps and the observability URLs on
completion. The full gating-chain rationale and the "why a plain `up` produces a
dead run" table live in the [dry-run runbook](agent-dry-run-runbook.md).

> The seeded experiment is on the **`game.windows`** flag (music-tempo pacing /
> "frantic music pacing windows"), objective `balanced`, `N=8` per arm
> (`docker-compose.dry-run.yml`). It is a **real FFA difficulty lever** (#1090),
> so the verdict is meaningful, not cosmetic.

### Point at real inference (optional, makes `mode=llm` real)

The stub backend decides on `mode=rules`. To exercise the **LLM** decision path,
configure an OpenAI-compatible backend **before** running the enable script (the
script only flips `mode → llm` when it sees `AGENT_INFERENCE_BACKEND=openai`):

```bash
# Ollama-direct (simplest). Recreate the agent with the backend env set
# (the AGENT_INFERENCE_* vars are read at process start, so this recreate IS
# required to pick them up):
AGENT_INFERENCE_BACKEND=openai \
AGENT_INFERENCE_BASE_URL=http://host.docker.internal:11434/v1 \
AGENT_INFERENCE_MODEL=phi4-mini \
  docker compose -f docker-compose.yml -f docker-compose.ci.yml --profile agent up -d agent

# then flip the gates (this run also flips mode -> llm because the env is set).
# The gate + mode flips take effect live (~1 s) — no further restart needed:
AGENT_INFERENCE_BACKEND=openai ./scripts/agent-dryrun-enable.sh on
```

- The `agent.json` `model` flag selects the route; variants are `phi4-mini`
  (default), `gemma3_4b`, `claude`, `copilot`. The agent sends that value as the
  OpenAI `model` param.
- **Reaching a host Ollama is the most common live foot-gun.** Ollama must bind
  `0.0.0.0` (not its `127.0.0.1` default), and under **WSL2 with Ollama on
  Windows** the container must dial the **WSL default-route gateway IP**, *not*
  `host.docker.internal`. Full checklist:
  [`agent-ollama-host-runbook.md`](agent-ollama-host-runbook.md).
- For provider routing + cloud fallback (`claude → phi4-mini`), use the optional
  LiteLLM gateway instead — see
  [`agent-inference-gateway.md`](agent-inference-gateway.md). The gateway owns
  the LLM fallback chain; the agent always keeps its own terminal "no backend →
  rules" rung, so a gateway/Ollama outage is **never** an agent outage.

### Observability URLs

The whole stack is proxied through envoy on port **80**:

| Tool | URL | Use on stage |
|------|-----|--------------|
| **Dashboard (SPA)** | `http://localhost/` | the demo front-of-house: **Agent** tab (decision feed) + **Experiments** tab (cohorts/verdicts) |
| **Jaeger** | `http://localhost/jaeger/` | agent decision-audit spans (service `agent`): `agent.signal_received → agent.decision → agent.action`, experiment declare/conclude/verdict spans |
| **Grafana** | `http://localhost/grafana/` | dashboards (see act 3 below) |
| **Prometheus** | `http://localhost/prometheus/` | `game_interventions_total`, agent process metrics |

### Pre-demo checklist

- [ ] `make dry-run` came up clean: `docker compose ... ps` shows all services
      healthy (give audio + agent a few seconds to pass their health checks).
- [ ] **Gates on**: `./scripts/agent-dryrun-enable.sh status` shows
      `enabled = on`, `experiments_enabled = on`, and `mode = llm` (real
      inference) or `rules` (stub) — whichever you intend to show.
- [ ] **Loop live** since the enable flip (the gate flip takes effect within ~1 s,
      self-gated per-tick since #1044 — no restart needed): the log shows live
      `experiment.*` activity, **not** `kill-switch: agent disabled`.
- [ ] **Inference reachable** (only if showing `mode=llm`): a throwaway curl from
      inside the agent network returns 200 + your model
      (`docker run --rm curlimages/curl -s http://<host>:11434/api/tags`), and
      the agent log shows `mode=llm`, not `no_backend_available`. Warm the model
      once so the first on-stage decision isn't a 30–100 s cold start (see
      failure modes).
- [ ] **Dashboards load**: `http://localhost/` Agent + Experiments tabs render;
      Grafana **Agent Operations — Fleet** and **Performance Experiment** open.
- [ ] **Known-good flag state**: gates set as above; production
      `services/flagd/agent.json` defaults **untouched** (the helper scripts
      only write the `ci/` dir).
- [ ] *(Real controllers only)* controllers paired and showing READY (bright
      LED).
- [ ] **Panic button rehearsed**: you can flip the kill-switch off in one
      command (see act 3 / failure modes).

---

## 2. What to show — the 3-act demo arc

### Act 1 — Experiments declaring + concluding (verdicts)

The agent runs a **cohort loop**: it declares the seeded experiment, spreads
arms across **shadow** games, scores them, and reaches a **verdict**
(`significant` / `inconclusive` via a Cohen-d effect size on the objective).

- **Watch it on**: the dashboard **Experiments** tab (`http://localhost/`) and
  the Grafana **Performance Experiment: Backend Comparison** dashboard; in Jaeger
  (service `agent`) the `experiment.declared` → `experiment.started` →
  `experiment.game_assigned` → conclude/verdict spans.
- **Talking point**: the real game default is **never touched** — promotion gates
  (`code_improvement.*`) stay off, so a concluded experiment yields a *verdict*,
  not a live default change. The agent measures on shadow substrate first.

### Act 2 — The agent making decisions + interventions

The agent OBSERVEs the live game via OTLP telemetry, DECIDEs each cycle (rules
engine, or the LLM when `mode=llm`), and — on the ACT path — applies bounded
interventions.

- **Watch it on**: the dashboard **Agent** tab (live Jaeger-polled decision
  feed) and Jaeger's `agent.decision` spans. Each decision span carries the whole
  `LayerState`: which flags were in effect, which objective was served, the chosen
  action + why, and whether it was permitted.
- **`mode=rules` vs `mode=llm`**: with real inference configured, decisions show
  `mode=llm` and the configured model; the stub shows `mode=rules`. Both produce
  the same span schema, so the demo arc is identical either way (the fallback to
  rules is itself a talking point — see act 3 / failure modes).
- **Interventions**: with the ACT path open (see the
  [ACT runbook](agent-act-runbook.md) — `AGENT_INTERVENTIONS_ENABLED=true` +
  `enabled=on` + a permitting `interventions_allowed` variant), a permitted
  decision rewrites `interventions.json` and the coordinator applies it,
  incrementing `game_interventions_total`. Shadow-only nudges like
  `set_player_handicap` / `player_handicap_factor` (#1107) demonstrate a
  per-player difficulty lever the agent can pull without touching the real game.
- **Drive it deterministically**: `tools/demo/demo_driver.py` plays scripted
  mock-controller movement engineered to trip a specific rule on cue — so the
  agent decides something *on demand* instead of you hoping it does. (It drives a
  real, menu-started game, not a shadow.)

### Act 3 — Live flag flips via flagd (the control plane is live)

Every gate is an OpenFeature flag; flagd hot-reloads file edits in **~100 ms–1 s**
with no restart. Show the control plane reacting live:

- **The kill-switch panic button** — the headline live flip. Flip `enabled` off
  and the loop short-circuits on its next cycle, emitting only a throttled
  `agent.disabled` / `kill-switch: agent disabled` trace:
  ```bash
  ./scripts/agent-killswitch.sh off    # instant brake, no restart
  ./scripts/agent-killswitch.sh on     # resume
  ```
- **Soften instead of stop** — set `interventions_allowed` to `none` in the agent
  flagd domain: the loop still runs and traces, but every decision is blocked
  `not_allowed` (visible as `decision.blocked=true`). See the
  [ACT runbook](agent-act-runbook.md#demo-flow--drive-behavior-live-and-watch-the-trace-change)
  for objective / fitness / rate-limit live flips that each change the **next**
  `agent.decision` span.
- **Fleet view** — the Grafana **Agent Operations — Fleet** dashboard (#791) is
  the wide "system is alive" shot for the audience.

> **Flag-edit gotcha (read before the talk):** the demo stack serves the
> `services/flagd/ci/` flag dir. The `agent-killswitch.sh` / `agent-dryrun-enable.sh`
> helpers already target `ci/` — *use them.* A hand-edit of the **base**
> `services/flagd/agent.json` will **not** take effect in the demo (flagd is
> serving `ci/`). See failure modes → *flag changes not taking effect*.

---

## 3. Failure modes & recovery

These are the **real** failure modes hit during dry-runs. Each is *symptom →
check → fix*. The recurring theme: the agent **fails safe** (degrades to rules /
inert) rather than crashing, so the symptom is usually "nothing happened," not an
error.

### A. Inference unreachable → `mode=rules` / `no_backend_available`

- **Symptom**: you expect `mode=llm` but agent decisions log `mode=rules` with
  `no_backend_available`; the LLM is never called.
- **Check**:
  ```bash
  docker compose logs agent | grep -E 'agent\.evaluate|no_backend_available' | tail
  # prove the network path from inside the agent's network:
  docker run --rm curlimages/curl -s http://<host>:11434/api/tags
  ```
- **Fix**: Ollama must bind **`0.0.0.0`** (not `127.0.0.1`), and under **WSL2 +
  Windows-Ollama** point `AGENT_INFERENCE_BASE_URL` at the **WSL default-route
  gateway IP** (`ip route show default | awk '{print $3}'`), *not*
  `host.docker.internal`. If the curl returns 200 but the agent still says
  `no_backend_available`, it's a **model** mismatch — `AGENT_INFERENCE_MODEL`
  must match a tag from `/api/tags` exactly (e.g. `phi4-mini`). Full checklist:
  [`agent-ollama-host-runbook.md`](agent-ollama-host-runbook.md).
- **Talking point**: this degradation is *by design* — the demo keeps running on
  the rules engine. A flaky LLM never takes the game down.

### B. Cold-start latency (~30–100 s on the first decision)

- **Symptom**: real inference is reachable, but the **first** `mode=llm` decision
  hangs for tens of seconds (the model is loading into memory).
- **Check**: agent log shows a long gap before the first `agent.llm.*` span;
  subsequent decisions are fast.
- **Fix**: **warm the model before the talk** and keep it resident —
  `OLLAMA_KEEP_ALIVE` (e.g. `OLLAMA_KEEP_ALIVE=30m` or `-1` to never unload) on
  the Ollama host, plus one throwaway inference request during setup so the first
  on-stage decision is hot.

### C. Experiments idle after the first one concludes

- **Symptom**: the seeded experiment runs and concludes, then the Experiments tab
  goes quiet — no new experiments appear.
- **Check**: `AGENT_EXPERIMENT_DYNAMIC_ENABLED` is `false` (the dry-run default),
  so **only the one seeded experiment** runs.
- **Fix**: that's expected. To keep a continuous stream for a longer demo, bring
  the stack up with dynamic experiment generation on:
  ```bash
  AGENT_EXPERIMENT_DYNAMIC_ENABLED=true make dry-run
  ```
  Or simply re-seed a fresh experiment on another flag before each run:
  ```bash
  AGENT_EXPERIMENT_SEED_FLAG=invincibility_seconds \
  AGENT_EXPERIMENT_SEED_VALUE=4.5 make dry-run
  ```
  (The seed flag must exist in `services/flagd/ci/game.json` and the value's JSON
  kind must match its variants.)

### D. `inconclusive` verdicts on the mock substrate

- **Symptom**: experiments conclude `inconclusive` — no significant effect-size
  signal.
- **Check**: the dry-run runs on **mock controllers** with synthetic, near-uniform
  movement, so arms often look statistically indistinguishable (little real signal
  to separate them).
- **Fix**: this is *honest* and worth narrating — the framework correctly reports
  "no signal" rather than inventing one. For a guaranteed `significant` verdict on
  stage, drive differentiated movement with `tools/demo/demo_driver.py`, or seed a
  flag whose arms produce a clearly different objective on the mock substrate. Do
  **not** pretend `inconclusive` is a failure of the agent.

### E. Kill-switch off → the loop is inert

- **Symptom**: nothing happens at all — no experiments, no decisions; agent log
  shows `kill-switch: agent disabled` / `agent.disabled` spans (`agent.enabled=false`).
- **Check**: `./scripts/agent-dryrun-enable.sh status` (or `agent-killswitch.sh
  status`) — `enabled` is `off`.
- **Fix**: flip it on — the gate flip takes effect **live** within ~1 s, no
  restart needed:
  ```bash
  ./scripts/agent-dryrun-enable.sh on
  ```
  (Both the kill-switch *flag* and the *experiment loop* gate are read live: the
  ACT path hot-reloads the flag, and since #1044 the loop is always built and
  self-gates each tick on the live `experiments_enabled` flag — an off→on flip
  starts it with no agent restart. A restart/recreate is only needed when you
  set/change the `AGENT_INFERENCE_*` env vars, which are read at process start.)

### F. Flag changes not taking effect (flagd `ci/` vs base dir)

- **Symptom**: you edited a flag but the agent's behavior didn't change.
- **Check**: did you edit `services/flagd/agent.json` (base) by hand? The demo
  stack serves the **`services/flagd/ci/`** dir, so base-dir edits are ignored.
- **Fix**: use the helper scripts — they write the `ci/` dir
  (`agent-killswitch.sh`, `agent-dryrun-enable.sh`), or edit
  `services/flagd/ci/<domain>.json` directly. This is the same class of dir
  mismatch the dry-run override fixes for the agent's own writes (#999/#822). For
  the corruption self-heal case (invalid `interventions.json` → flagd rejects the
  whole set), see the
  [ACT runbook → interventions file corruption](agent-act-runbook.md#recovery--interventions-file-corruption-924).

### G. A service is unhealthy / a recovery restart

- **Symptom**: `docker compose ... ps` shows a service unhealthy, or the agent /
  flagd / Jaeger stopped responding.
- **Check**: `docker compose ... logs -f <service>`.
- **Fix**: restart just that service (the dry-run compose file set):
  ```bash
  docker compose \
    -f docker-compose.yml -f docker-compose.override.yml \
    -f docker-compose.ci.yml -f docker-compose.dry-run.yml \
    --profile agent --profile dashboard restart <service>
  ```
  After restarting **flagd**, the agent re-reads flags within ~1 s (no agent
  restart needed). After restarting the **agent**, re-confirm the gates are still
  `on` (`agent-dryrun-enable.sh status`).

### Worst case — the demo cannot recover live

If the agent / inference path is wedged on stage and a restart doesn't clear it,
**fall back to the rules engine** — it needs no LLM, no network, no hardware:

- The stack with mock controllers + `mode=rules` is the always-available baseline.
  Re-run `make dry-run` + `agent-dryrun-enable.sh on` (without an inference
  backend) and demo acts 1–3 on rules. The decision arc and span schema are
  identical; only `mode=rules` vs `mode=llm` differs.
- If the whole live demo is unrecoverable, switch to **pre-captured Jaeger traces
  / dashboard screenshots** and narrate the architecture from them. Keep a recent
  set captured during your dry-rehearsal for exactly this.

---

## 4. Teardown

Tear down the dry-run stack and restore the fail-closed flag defaults:

```bash
docker compose \
  -f docker-compose.yml -f docker-compose.override.yml \
  -f docker-compose.ci.yml -f docker-compose.dry-run.yml \
  --profile agent --profile dashboard down

./scripts/agent-dryrun-enable.sh off    # restore enabled / experiments_enabled / mode -> off / off / rules (ci dir)
```

`agent-dryrun-enable.sh off` only resets the `services/flagd/ci/` dir; the
production `services/flagd/agent.json` defaults were never touched. Confirm with
`./scripts/agent-dryrun-enable.sh status`. If you brought up the optional LiteLLM
gateway, add its compose file to the `down` and unset the inference env you set on
the host.
