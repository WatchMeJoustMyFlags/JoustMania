# Talk runbook: "Two Standards Walk Into an Agent"

**Runtime Agent Control with OpenTelemetry and OpenFeature.** The operator
script for the live demo (issue #743, epic #1225).

> Autonomous agents are being deployed without runtime control. No open,
> vendor-neutral way to change what they do. No visibility into why they
> decided. We can fix that.

The demo is a physical party game (JoustMania, PS Move controllers) on a
Raspberry Pi 5. An agent watches the game only through OpenTelemetry and is
steered only through OpenFeature flags served by flagd. On stage we show:
observing a decision, switching the model live, tightening permissions, the
kill switch, rolling back without a redeploy, degrading to rules when the
network goes away, and the post-game summary.
The experiments / shadow-games / dark-factory material from
[NORTHSTAR.md](../NORTHSTAR.md) is **not** in this talk (the experiments loop
is described in [agent-dry-run-runbook.md](../agent-dry-run-runbook.md)).
Shadow games appear only as the insurance when Bluetooth fails at the venue:
see [Fallback: Bluetooth down](#fallback-bluetooth-down).

**Backend roles** ([C-018](../requirements.md#constraints)):

| Backend | Role on stage | How it is selected |
|---|---|---|
| **Jev** (TypeSafe, `jev-latest`) | The **main in-game backend**: a gated two-stage round, about 0.22-0.34 s for a "no action" round, 0.47-0.57 s for an act round | `agent-backend.sh jev` (flag `decision.backend`) |
| **Claude** | The **backup backend** in game (one manual flip, no automatic jev -> claude chain), and the **post-game summary** (retro; the proposer belongs to the experiments act and stays off) | backup: `agent-backend.sh claude`; summary: flag `inference.backend=claude` (in the stage baseline, applied by `talk-stage.sh`; needs `ANTHROPIC_API_KEY`) |
| **rules** | The **automatic floor**: any remote failure (no key, timeout, error, invalid answer, budget) falls back to rules in the same cycle; the game never waits | always; also `agent-backend.sh rules` |

The hardware proof on the real power bank is the [stage test checklist](stage-test-checklist.md).

Every command below was checked against the code on `main`. Anything that
needs the demo hardware, live keys or a stopwatch is marked
**verify in rehearsal**.

---

## 1. Setup

### Hardware

- Raspberry Pi 5 running the stack (Docker + Compose v2.24+).
- 2-4 PS Move controllers, paired and charged
  ([hardware setup guide](../hardware-setup-guide.md)). Mock controllers are the
  fallback, see "Two stacks" below.
- Presenter laptop with a browser on the Pi's address. The Pi has no screen of
  its own on stage.
- Phone with a hotspot (for NTP and as a WiFi fallback, see the pre-flight list).

### Two stacks: pick one, then never mix them

The helper scripts change flags **only through the flag service** (epic #1407):
`http://localhost/flags/...` through envoy, the same on both stacks. Every stage
change is an in-memory overlay (`writer=stage`); no flag file is edited, and a
restart of the flag service drops the overlays (run `talk-stage.sh reset` again).
The exception is the decision backend and its model: `agent-backend.sh`
writes them **persisted** (#1682), as the operator's choice, so they survive a flag-service restart or a reboot. A stage
overlay over a persisted backend is temporary: a restart brings the persisted
backend back. `talk-stage.sh reset` clears the persisted backend and model too,
and `talk-preflight.sh` WARNs while a remote backend (jev / claude / openai) is
persisted.
`./scripts/talk-preflight.sh` checks that flagd holds its sync stream to the
flag service (the "flag service" section).
Its controller-backend check (1b) reads the `backend` flag the **running** flag
service serves, so it follows the baseline that stack loaded: on the rehearsal
stack (`services/flagd/ci/`, `backend=mock`) it prints INFO `rust-hid not
needed`, which is expected there; only the stage stack (`services/flagd/`,
`backend=rust`) proves rust-hid is running and healthy (#1402).

| | Stage stack (Pi, real controllers) | Rehearsal / fallback stack (mock controllers) |
|---|---|---|
| Bring-up | `./scripts/setup/joustmania-compose.sh up -d` (= `docker compose -f docker-compose.yml --env-file .env --env-file .env.local up -d`; also what systemd autostart runs, #1488) | `COMPOSE_ENV_FILES=.env,.env.local make dry-run` |
| Compose files | `docker-compose.yml` only, named with `-f` so the dev `docker-compose.override.yml` is **not** loaded (it publishes the gRPC and mock control ports on the venue network, #1319) | `docker-compose.yml` + `override` + `ci` + `dry-run`, profiles `agent` + `dashboard` |
| Baseline the flag service loads (read-only) | `services/flagd/` | `services/flagd/ci/` |
| Gameplay | real players | `tools/demo/demo_driver.py` (scripted mock movement) |
| Infra rollback beat | possible (needs the python HID adapter) | not possible (see section 4) |

Notes:

- No helper and no service writes a flag file any more: the baseline files stay
  as checked in. The agent's interventions are per game and die with the game;
  its rollout moves (infra beat) are in-memory overlays. The backend and model
  flips are persisted (see above); every other stage change is in memory.
- `make dry-run` also sets the experiment env vars, but the live gate
  `experiments_enabled` stays `off`, so the experiment loop and the proposer are
  inert. Keep it off.
- `COMPOSE_ENV_FILES` needs a Compose version that supports it; if in doubt use
  the explicit form from the `make dry-run` recipe with
  `--env-file .env --env-file .env.local` added. **Verify in rehearsal.**

### Keys and env: `.env.local`, never `.env`

`.env` is tracked in git and pins `IMAGE_TAG=0.7.0`, which predates the
decision backends. Put everything below in the git-ignored `.env.local`
(later `--env-file` wins). `scripts/setup/joustmania-compose.sh` passes both
env files (`.env.local` only when it exists) to every compose call, and the
systemd autostart uses it too, so a reboot comes back with the same tag and
keys as the manual bring-up (#1488). Rehearse by pinning an exact commit sha
(the bare 40-hex tag, not `sha-...` and not `latest`; UC-060 BR-003) and keep
that checkout for the talk, so a new push to `main` cannot change the stage
images. The image tag derives from the checked-out commit; do not set
`IMAGE_TAG` in `.env.local` on the Pi (it is ignored with a status WARN):

```bash
# .env.local  (git-ignored; never commit, never paste on screen)
JEV_API_KEY=...                        # Jev, the main in-game backend
ANTHROPIC_API_KEY=...                  # Claude: in-game backup AND the post-game summary
# DECISION_OPENAI_API_KEY=...          # only for the optional second-vendor beat (6b)
# AGENT_LLM_MAX_REQUESTS_PER_MINUTE=30 # optional ceiling on the budget flag (can only lower it), see "Budget"
```

Pin and migrate with the upgrade command, never with a hand-made `git checkout`:
`./scripts/setup/joustmania-upgrade.sh <joustmania-vX.Y.Z | sha>` pulls first, switches, restarts and rolls
back when the game is not healthy; `--rollback` returns to the previous pin offline (UC-060 A2, A3). One-time
migration of a Pi still on `main`: `git fetch --tags && ./scripts/setup/joustmania-upgrade.sh <first pinnable>
&& sudo ./scripts/setup/install_autostart.sh` ([DEVELOPMENT.md](../DEVELOPMENT.md#upgrade-rollback-and-the-one-time-migration-uc-060-a2-a3)). Stack
down after a run that says `refused by systemd (start-limit-hit)` (or `--status` shows it): `sudo systemctl
reset-failed joustmania && sudo systemctl start joustmania` (UC-060 BR-006).

The agent reads all of these **once at startup**. (The post-game summary backend is no longer here: it is the flag `inference.backend`, `claude` in the stage baseline, #1810; with flagd down the agent runs the stub, #1907.) Changing one means
recreating the agent:
`./scripts/setup/joustmania-compose.sh up -d agent` (use the
dry-run `-f` files and profiles on the rehearsal stack). With
the flag `inference.backend=claude` but no `ANTHROPIC_API_KEY` the agent logs
`inference.backend=claude but ANTHROPIC_API_KEY is empty` (once per change) and the summary
stays capture-only.

Jev needs only `JEV_API_KEY`: `decision.jev.base_url` defaults to
`https://api.typesafe.ai/v1` and `decision.jev.model` to `latest` (`jev-latest`).
The gate calibration below was measured on `jev-1.13.0`; if `jev-latest` moves
before the talk, pin it with `./scripts/agent-backend.sh jev pinned` **after** the
T-1h `talk-stage.sh reset` (the reset clears a persisted model) and re-check the act/noop beat in rehearsal.

**Budget.** Remote calls share one global per-minute budget (a sliding 60 s
window): every remote decision (one slot per decision; a Jev act round's two
HTTP calls count once) and every post-game Claude summary (retro). The cap is
the `llm.max_requests_per_minute` flag (`conservative` 3, `default` 6,
`aggressive` 12, `stage` 30), capped by `AGENT_LLM_MAX_REQUESTS_PER_MINUTE`
when that is set: the cap is `min(env, flag)`, so the env can only lower the
flag, never raise it (#1788, `ceilBudget`, `services/agent/decision/llmbudget.go`). One game on a remote
backend at `decision.min_interval_ms=3000` (plus `decision.min_gap_after_answer_ms`,
1000 ms after each answer, #2123) wants about 20 calls a minute on Jev, about 15 on
warm Claude and 6 to 7 when Claude runs into the 8 s `patient` timeout, so the
`stage` budget (30) leaves room for the summary, while the default budget runs out after six calls: the rest fall back to rules
with `decide.fallback_reason=llm_budget_exhausted`, and the summary at game end
finds the budget full. For the talk use the **`stage` variant** (30/min, a live
flag flip, part of the baseline in section 6); leave the env unset or at 30 or
more, since a lower env value caps `stage`.

### The second vendor (optional beat 6b: fill in before T-24h or cut it)

`decision.openai.model` ships with only an empty `unset` variant and
`decision.openai.base_url` with only `default` (`https://api.openai.com/v1`).
The model takes a free string at runtime. The base URL does **not**: it carries the
API key (`Authorization: Bearer`), and `/flags/` has no authentication, so the
registry accepts only declared variants. Add a `vendor` variant with your URL to
`decision.openai.base_url` in the baseline file (`services/flagd/agent.json`, or
`ci/agent.json` on the rehearsal stack), run `./scripts/flags.sh restart`, then:

```bash
./scripts/flags.sh set-value agent decision.openai.model '"<VENDOR_MODEL_ID>"' --writer stage
./scripts/flags.sh set agent decision.openai.base_url vendor --writer stage
```

- `gen_ai.provider.name` is derived from the base URL host
  (`services/agent/decide/backends/openai/provider.go`): the semconv
  well-known value for OpenAI, Gemini, Vertex AI, Mistral, Groq, DeepSeek, xAI,
  Perplexity, Cohere, Azure, Bedrock and IBM watsonx hosts. Unknown hosts
  (self-hosted, other vendors) record `openai`.
- If the model rejects `reasoning_effort=low`, run
  `./scripts/agent-backend.sh effort omit`.

### The blackhole variant for the degrade beat

`decision.jev.base_url` ships a non-default `blackhole` variant,
`https://192.0.2.1/v1` (both baseline files, [flags spec](../specs/flags.md)).
192.0.2.0/24 is TEST-NET-1 (RFC 5737): never routed, so the call cannot reach
anything on the venue network, and it is `https`, so the TLS handshake fails
before any request header (the Jev bearer key) is sent. A call to it usually
hangs until `decision.jev.timeout_ms` (default 2500 ms) expires; a network that
rejects it fast gives `error` instead. The local network stays up, so the
presenter keeps the view of the Pi (beat 7, option B). Never point a base URL
variant at private (RFC 1918) or plain-`http` addresses: the key would travel
to whatever answers there.

### A helper for flags that `agent-backend.sh` does not cover

`agent-backend.sh` flips the `decision.*` flags (backend, model, both act
thresholds, capture, effort) and `agent-killswitch.sh` flips `enabled`. For the
rest (`interventions_allowed`, `interventions_enabled`,
`decision.min_interval_ms`, `decision.jev.base_url`, `decision.*.timeout_ms`,
`llm.max_requests_per_minute`, rollout flags) use `scripts/flags.sh`, or paste
this shorthand into the presenter shell:

```bash
# setflag <set> <flag-key> <variant>: a stage overlay; the flag service refuses
# read-only flags (422 flag_read_only) and unknown variants (422 out_of_bounds)
setflag() { ./scripts/flags.sh set "$1" "$2" "$3" --writer stage; }
```

The flag service pushes the change to flagd at once; the agent reads every flag
used below at use time, so **no restart** for any flag flip. All commands run in
an **ssh session on the Pi**, in the repo root (they call
`http://localhost/flags/...`; set `FLAG_SERVICE_URL` to call it from the laptop).

**Show the flag change, not only its effect.** The OpenFeature half of the talk
is only visible if the audience sees the flag move. Keep the terminal next to
the dashboard and, after each flip, run `./scripts/agent-backend.sh status`
(it shows backend, model, the global threshold and Jev's own threshold) or
`./scripts/flags.sh get agent decision.backend`. Every flip is also a
`flagservice.write` span in Jaeger (service `flag-service`), next to the
decisions it changes.

### URLs (through envoy on port 80)

Replace `localhost` with the Pi's address from the presenter laptop.
Unpair on a controller card (UC-005) only works when the dashboard is opened by
the Pi's IP, `localhost`, the Pi's host name or `<host name>.local` (read from
the host's `/etc/hostname`); any other name (a DNS alias, a router-assigned
name) answers 403 until it is added to `JOUSTMANIA_HOSTNAMES` in `.env.local`
(comma-separated) and the connect-proxy is recreated (DNS-rebinding guard, #1450).

| Tab | URL |
|---|---|
| Dashboard, **Agent** tab, **Decisions** panel (#1246) | `http://localhost/` |
| Jaeger, agent.decide search | `http://localhost/jaeger/search?service=agent&operation=agent.decide&lookback=15m&limit=20` |
| Jaeger, held decisions only | `...&tags=%7B%22decide.outcome%22%3A%22held%22%7D` |
| Jaeger, post-game summary (beat 8) | `http://localhost/jaeger/search?service=agent&operation=agent.llm.retro&lookback=30m&limit=5` |
| Jaeger, infra decisions (section 4) | `http://localhost/jaeger/search?service=agent&operation=agent.infrastructure.decision&lookback=15m` |
| Grafana, **stage view** | `http://localhost/grafana/d/talk?kiosk` (**Talk: Two Standards**, folder Talk, #1427): the one Grafana screen for this talk and Grafana's home page, eight large panels, refresh 10 s, no variables. Big numbers: live games (real / shadow), agent decisions per minute (the collector's span metrics over `agent.decide`: OTel in), OpenFeature flag evaluations per second (the standard OpenFeature OTel hook metrics `feature_flag_evaluation_*`) and flag-service writes per minute (every flip below is one: OpenFeature out). Below them: decisions per minute by `decide.backend` · `decide.outcome`, interventions applied per minute by type, evaluations per second by flag, and recent `agent.decide` traces from Jaeger. Use it next to the dashboard's Agent tab, which stays the decision-by-decision view |
| Grafana, flags | `http://localhost/grafana/d/feature-flags` (**Flags / OpenFeature**, folder Platform): evaluations by flag, variant and reason, evaluation errors and fallbacks, the flag service (writes, live games registered, flagd sync streams, snapshot size) and flagd health |
| Grafana, agent metrics | `http://localhost/grafana/d/agent-operations` (**Agent Decisions & Interventions**, the fleet view; replaced Agent Operations — Fleet and Interventions in #1427). Row Decide: decisions per minute by `decide.backend` and the outcome split (act / noop / held / fallback), derived by the OTel collector's span-metrics connector from the `agent.decide` spans, so a backend flip shows within a minute; row Remote Backends: `gen_ai.client.operation.duration` p95 and tokens by model |
| Grafana, game and players | `http://localhost/grafana/d/game-players` (**Game & Players**: live games, games by outcome, game-loop health, and a per-player drill-down row with the 100 Hz acceleration against the warning and death lines; replaced Game Quality, Game Analytics and Player Insights in #1427) |
| Grafana, service and host health | `http://localhost/grafana/d/joustmania-service-health` (**Infrastructure & Services**; absorbed the separate Fleet Overview dashboard in #1427) |
| Grafana, controllers and Bluetooth | `http://localhost/grafana/d/controller-overview` (**Controllers & Bluetooth**; the dashboard's Metrics tab embeds it. Expand the collapsed "Bluetooth and Adapters (Pi only)" row for per-adapter load and signal before deciding on the [Bluetooth fallback](#fallback-bluetooth-down)) |

The Jaeger search URL parameters are standard Jaeger UI query parameters;
**verify in rehearsal** that the pre-filled search opens directly.

### What is on screen

- **Decisions panel** (dashboard, Agent tab): polls Jaeger every 1.5 s for the
  newest `agent.decide` spans. The query never uses the browser clock (it relies
  on Jaeger's own "now"), so a skewed presenter laptop does not empty it. The
  current-backend strip shows backend, model and act threshold, taken from the
  newest `agent.decide` span, and flashes when one of them changes. The
  threshold is only on spans that were routed (an `act` or `held` row): until
  the new backend produces one, the strip shows "—" or a dimmed value from an
  older row (e.g. a dimmed 0.60 from Jev right after the flip to Claude). On `rules`
  it only updates when rules decide something, so a flip *to* rules shows on
  the next rules decision, not instantly. One
  row per decision: outcome badge (act green, held amber, noop grey, invalid
  purple, fallback orange, discarded dashed), backend (struck through with
  `-> rules` and the reason on a fallback), model, answer, latency, `trace` link.
- **`agent.decide` span** (Jaeger, service `agent`): `gen_ai.agent.name`,
  `gen_ai.conversation.id` (= the game), `gen_ai.request.model`,
  `decide.backend` / `decide.backend.requested`, `decide.fallback_reason`,
  `decide.outcome`, `decide.discarded_reason`, `decide.confidence`,
  `decide.act_confidence_threshold` and `decide.act_confidence_threshold.source`
  (`jev` or `global`), `decide.rationale` (the "why"), `decide.answers`,
  `decide.latency_ms`, `error.type`, and a `gen_ai.evaluation.result` event
  ("confidence 0.720 >= threshold 0.600 (decision.jev.act_confidence_threshold)").
- **`chat {model}` CLIENT spans** (children of `agent.decide`, remote backends
  only): `gen_ai.provider.name` (`jev`, `anthropic` or the vendor), request and
  response model, token usage, and with `decision.capture_content=on` the
  prompt and output messages. Jev makes one span per stage (`jev.stage` =
  `intervention` / `details`); the stage-1 span carries Jev's real gate answer:
  `jev.gate.p_yes`, `jev.stage1.choice`, `jev.stage1.distribution`.
- **`agent.llm.retro` span** (beat 8, its own trace, linked to the game trace
  and tagged `game.trace_id`): the post-game summary, see beat 8.

How decisions are produced: on `decision.backend=rules` an `agent.decide` span
appears only on cycles where the rules engine decides something. On a remote
backend the agent asks at most every `decision.min_interval_ms` per game, only
for **real** games (`llm.eligible_game_kinds=real_only`), and only while a game
is running and sending telemetry. **No game running = no new rows.** Keep a game
going during every beat.

**How Jev decides** ([decide contract §3.1](../specs/decide-contract.md)). Each
Jev round first asks a yes/no gate, "does the game need an intervention now?",
and in the same call which intervention would fit. Jev's answer `p_yes` is the
decision's confidence. Below 0.5 the answer is `noop` (one call, grey row);
above 0.5 Jev picks the target and strength in a second call, and the agent
compares `p_yes` with Jev's own threshold `decision.jev.act_confidence_threshold`
(default `gate` = 0.6, #1288): `act` at or above it, `held` below. Calibrated
gate values (jev-1.13.0): calm about 0.16, dragging about 0.35, collapsing about
0.68, near the death threshold about 0.72. So on the default bar Jev **acts**
when players are near the threshold or eliminations collapse the game, and
stays **noop** while the game is calm or dragging. Narrate a noop row by its
confidence: `noop` at 0.84 reads "84% sure nothing is needed" (confidence is
1 − p_yes for `noop`).

Do not promise exact numbers on stage. The calibration states are fixtures
asked with the `full` allow-list; on stage the agent asks with `standard`
(the stage baseline, #1301), so the pick is one of `play_audio_cue`,
`send_controller_effect`, `adjust_volume`, `adjust_music_tempo`,
`set_pacing_profile`, `adjust_player_sensitivity` or `grant_shield`, and
real (or mock) motion is noisier than the fixtures (mock motion noise: #1279).
Say "around 0.7 near the threshold", and measure it in rehearsal (section 9). The global
`decision.act_confidence_threshold` (default 0.8) governs claude and openai
only; it moves Jev's bar only when Jev's flag is `inherit`.

**Duration-driven decisions (#1305).** Since #1305 the agent sees the real elapsed game time (before, `elapsed_s` was 0 for the whole game), so the
duration-gated rules fire in real games. With the defaults (accelerate target 60 s, endurance minimum 120 s,
variance window 10 s):

| Elapsed | Rule | Intervention | Needs |
|---|---|---|---|
| under 120 s, with eliminations | R1 / R2 endurance | slow the tempo / calming audio cue | `standard` (tempo), `ambient` (cue) |
| from 10 s | R8 statue | controller effect on a player with near-zero movement variance (held mock controllers) | `ambient` |
| after 60 s | R5 accelerate | speed up the tempo | `standard` |
| after 90 s (3+ players) | R6 accelerate | eliminate the least-moving player | `full` |
| after 120 s, accelerate dominant | R7 accelerate | end the game | `full` |

Jev and Claude also get the real `elapsed_s` now, so their pacing answers can change after 60/90/120 s.
`full` (and `shadow_experimental`) lets the agent eliminate players and end the game: do not leave it set on a
real game unless that is the point of the beat.

---

## 2. Talk timing (30-minute slot, about 15 minutes live)

Beats marked **cut** can be dropped first; the order of cutting is I (infra),
6b, 3b, then shorten 2 to one act and one noop row. With all three cut the live
part is about 11 minutes.

| Time | Segment | Live? | Cuttable |
|---|---|---|---|
| 0:00 | Problem: agents ship without runtime control (abstract) | no | |
| 3:00 | The game and the architecture: OTel in, OpenFeature out, no control plane | no | shorten |
| 6:00 | Beat 1: observe a decision (rules) | yes, 1.5 min | |
| 7:30 | Beat 2: live switch to Jev, act vs noop | yes, 2.5 min | shorten |
| 10:00 | Beat 3a: Jev's threshold strict -> held -> gate | yes, 1.5 min | |
| 11:30 | Beat 3b: interventions_allowed ladder | yes, 1.5 min | **cut** |
| 13:00 | Beat 4: the backup, flip to Claude | yes, 1 min | |
| 14:00 | Beat 5: kill switch, including an in-flight discard | yes, 1 min | |
| 15:00 | Beat 6: rollback, flip back to Jev | yes, 0.5 min | |
| 15:30 | Beat 6b: second vendor (OpenAI-compatible) | yes, 1 min | **cut** |
| 16:30 | Beat 7: degrade, never stall | yes, 1.5 min | |
| 18:00 | Beat I: infra rollout, fitness violation, auto-rollback | yes, 2 min | **cut** (hardware only) |
| 20:00 | Beat 8: the post-game summary by Claude | yes, 1.5 min | |
| 21:30 | Takeaways: what OTel gave us, what OpenFeature gave us | no | |
| 26:00 | Q&A (buffer: overruns eat this first) | | |

---

## 3. Beat table

**Session baseline: `decision.backend=rules`** (section 6). The talk starts on
the floor on purpose: rules need no network and no key, so the opening beat
cannot fail, and beat 2's flip to Jev is the first visible OpenFeature moment.
Start state: the baseline, a real game running. `backend.sh` below is
`./scripts/agent-backend.sh`.

| # | Talk beat | Command | Flag changed | Expected on screen (dashboard / Jaeger / game) | Expected latency | Fallback if it doesn't show |
|---|---|---|---|---|---|---|
| 1 | **Observe**: the agent decides; why + confidence | (none; play) `backend.sh status` on screen | none | Dashboard: rows with backend `rules`, outcome `act`. Jaeger: open a row's `trace` link; `agent.decide` with `decide.rationale`, `decide.confidence`, `gen_ai.agent.name`; on the rules path the `agent.decision` spans (action, allowed/blocked) are its **siblings** under `agent.signal_received` | a row per rules decision; rules decisions depend on play. **Verify in rehearsal** how often a 4-player game decides | Run `demo_driver.py --scenario statue` (mock stack) or ask a player to freeze; show a pre-captured trace |
| 2 | **Live model switch to Jev (the main in-game backend)**: acts when the game needs it, stays out when it does not | `backend.sh jev` | `decision.backend=jev` | Strip flashes to `jev` / `jev-latest`; the threshold (0.60) appears with the first `act` / `held` row (before that "—" or dimmed). Calm play: grey `noop` rows ("84% sure nothing is needed"). Ask the players to push to the edge (near the death threshold) or let eliminations pile up: a green `act` row and something the audience notices: `grant_shield` turns the player's LED and dashboard tile white with a `Shield 5.0s` badge, fades, blinks in the last second and returns to the player colour, with the shield sound; `adjust_music_tempo` speeds the music up; `play_audio_cue` plays the lobby ping. Jaeger: the act trace has two `chat jev-latest` children (`jev.stage=intervention`, then `details`); the stage-1 span shows `jev.gate.p_yes` (around 0.7 near the threshold, around 0.16 calm; measure, do not promise), the event says e.g. `confidence 0.720 >= threshold 0.600 (decision.jev.act_confidence_threshold)`, source `jev` | flag-service push (p95 200 ms, NFR-050) + next decide slot (up to 3 s) + call: noop round about 0.22-0.34 s, act round about 0.47-0.57 s | `~~jev~~ -> rules, backend_unavailable`: `JEV_API_KEY` missing, see failure modes. `timeout` / `error`: Jev unreachable, go to beat 4 now (Claude backup). No `act` ever: mock stack `demo_driver.py --scenario death_cascade` (collapse) or `skill_gap` (novices in the danger zone); show a captured act trace |
| 3a | **Permissions: confidence threshold** (Jev's own bar) | `backend.sh jev-threshold strict`; after a held row: `backend.sh jev-threshold gate` | `decision.jev.act_confidence_threshold=strict` (0.95), then `gate` (0.6) | The near-threshold / collapsing rows that acted in beat 2 turn amber `held`: event "confidence 0.720 < threshold 0.950 (decision.jev.act_confidence_threshold)", nothing acts in the game. Calm / dragging rows stay grey `noop` (the gate is closed, p_yes ≤ 0.5, so there is nothing to hold). Back on `gate`: acts again | flag-service push (p95 200 ms, NFR-050) + the next decision that opens the gate | `held` needs an open gate: keep the players at the edge. The **global** `backend.sh threshold strict` does NOT move Jev (only claude/openai), so do not use it here. If no held row appears, show the evaluation event of a held captured trace |
| 3b | Permissions: what it may do (**cut**) | `./scripts/talk-stage.sh permissions none 12` (holds `none` 12 s, then restores `standard`; Ctrl-C restores at once) | `interventions_allowed` | No new rows: **the permission shrinks the question the model is asked (remote backends)**, and with `none` the only option left would be `noop`, so the agent does not ask at all (no remote call, no budget spent; `agent_llm_gated_total{reason=no_interventions_allowed}` counts the skipped cycles, #1298). The `ffa.intervention` options are `noop` plus the allowed list (`decide.FFAQuestions`, visible in `decide.questions`). Back on `standard` Jev is offered shield, tempo, cue and the rest again | flag-service push (p95 200 ms, NFR-050) + next decision | The helper restores `standard` on every exit but `kill -9`; if the beat was killed, `./scripts/talk-stage.sh reset` (the pre-flight FAILs on `none` / `probe`) |
| 4 | **The backup backend: flip to Claude** (manual, one flag; no automatic jev -> claude chain) | `backend.sh claude` (haiku) | `decision.backend=claude` | Strip flashes to `claude` / `claude-haiku-4-5-20251001`; the threshold may still show a dimmed 0.60 from Jev's last routed row until Claude's first `act` / `held` row shows 0.80 (the global one, source `global`). New rows backend `claude`; Jaeger child `chat claude-haiku-4-5-20251001` with `gen_ai.provider.name=anthropic`. Same contract, same rows, slower: point at `decide.latency_ms` (about 3 s vs Jev's 0.3-0.5 s) | flag-service push (p95 200 ms, NFR-050) + next slot + about 2.7-3.0 s (Haiku warm) | `backend_unavailable`: `ANTHROPIC_API_KEY` missing. `timeout` (above `decision.claude.timeout_ms`, default 5000 ms): `setflag agent decision.claude.timeout_ms patient` (8000) |
| 5 | **Kill switch**, including a discarded in-flight answer | `./scripts/agent-killswitch.sh off` right after a new Claude row appears (rows appear when a call lands; with about 3 s calls on a 3 s cadence, counted from each call's start, the next call is usually already in flight, so a discard is likely, about 3 in 4 tries; measure in rehearsal); after the point: `./scripts/agent-killswitch.sh on` | `enabled=off`, then `on` | No new `agent.decide` rows; Jaeger shows throttled `agent.disabled` spans. The in-flight call lands as a dashed `discarded` row with `kill_switch` (`decide.outcome=discarded`, `decide.discarded_reason=kill_switch`; its rules fallback is suppressed too). Game keeps playing, agent silent. Say what it stops (below) | flag-service push (p95 200 ms, NFR-050) + the next agent cycle | The discard is likely on Claude (about 3 in 4 tries), rare on Jev (its call is over in 0.3 s): do this beat while on Claude. If no discarded row: narrate the silent feed. **Verify in rehearsal** |
| 6 | **Rollback without redeploy**: back to Jev | `backend.sh jev` | `decision.backend=jev` | Strip back to `jev`; a Claude answer that lands after the flip shows `discarded` / `backend_changed`. No container restarted (`docker ps` uptime). The full rollback to the floor is one more flip: `backend.sh rules` | flag-service push (p95 200 ms, NFR-050) + the next agent cycle | If Jev is unreachable at the venue, stay on Claude and say so: this *is* the backup working |
| 6b | Second vendor, same contract (**cut**) | First `setflag agent decision.openai.base_url vendor`, **then** `backend.sh openai vendor`; afterwards `backend.sh jev` | `decision.backend=openai`, `decision.openai.model`, `decision.openai.base_url` | Backend `openai`, model `<VENDOR_MODEL_ID>`, `gen_ai.provider.name` changes to the vendor's value | as beat 4; **measure** (`decision.openai.timeout_ms` default 2500 ms) | `backend_unavailable`: model/base URL variant or `DECISION_OPENAI_API_KEY` missing. `invalid_answer` or `error`: `backend.sh effort omit`. Else skip |
| 7 | **Degrade, never stall** (on Jev) | **Option B (preferred):** `setflag agent decision.jev.base_url blackhole` (the shipped TEST-NET-1 variant). Option A: switch off the Pi's uplink (hotspot off / unplug WAN) | `decision.jev.base_url` (B) or none (A) | B: rows `~~jev~~ -> rules`, `fallback_reason=timeout` once `decision.jev.timeout_ms` expires (or `error` if the network rejects the address fast). A: usually `timeout` too (the kept-alive connection hangs), `error` only on a fast DNS / connect failure. Both: `error.type` set, span status error. Game keeps playing; rules keep deciding in the same cycle | B: 2.5 s (`decision.jev.timeout_ms` default). A: up to 2.5 s | Option B keeps the local network and the presenter's view of the Pi. Afterwards `setflag agent decision.jev.base_url default`; with option A **restore the uplink before the game ends**, or the closing summary (beat 8) fails |
| I | Infra: rollout -> fitness violation -> auto-rollback (**cut**, hardware only) | see section 4 | `rollout_enabled`, `remediation_allowed`, `target_backend`, `strategy` | `agent.infrastructure.decision`: `remediation.action=expand` then `rollback`, `fitness.violations` populated, `rollout.dry_run=false` | expand on the first passing health cycle (about 1 s), rollback a few seconds later; **verify in rehearsal** | Skip; show `TestInfraNarrative_FullIncident` or captured spans |
| 8 | **Closing: Claude's post-game summary** | Let the running game end (or end it from the menu); open the Jaeger retro tab | none | Jaeger `agent.llm.retro` (own trace, linked to the game; `game.trace_id` tag): `inference.used=claude`, `llm.retro.parse_ok=true`, `llm.retro.session_assessment` and `llm.retro.session_focus` (the summary), `llm.retro.suggestion_count`, one `retro.suggestion` event per suggested improvement (`intervention_type`, `emphasis`, `reason`), child `chat claude-opus-5-5` (`gen_ai.provider.name=anthropic`). Point: Jev decides in game in 0.3 s, Claude reflects afterwards; same telemetry, same flags | after game end: the retro model call (default `claude-opus-5-5`, effort medium; **measure in rehearsal**, budget up to 120 s) | Have a game end earlier (after beat 6) so its summary is already there. No retro span at all: the budget was full at game end (only the log `agent.llm.retro_gated fallback_reason=llm_budget_exhausted`). `inference.used=none`: `no_backend_available` (env not set) or `agent_disabled` (kill switch off at game end). `inference.used=claude` with `llm.retro.parse_ok=false`: the call failed (`llm.infer.error`, error status, log `agent.llm.retro_failed`). See failure modes |

**What the kill switch stops (beat 5, one sentence each on stage).** `enabled=off`
stops the game agent: no in-game decisions on any backend (rules included), an
in-flight remote answer is discarded when it lands (`kill_switch`) instead of
acting, and post-game model calls stop too (a retro is capture-only with
`inference.fallback_reason=agent_disabled`; the proposer does not run, #1284).
It does **not** stop the game, does not undo interventions already written for
the running game (they end with the game), and does **not** stop the infrastructure loop, which has
its own flag, `rollout_enabled` (section 4): each loop has its own switch.

---

## 4. Beat I: infra rollout and auto-rollback (optional, hardware only)

Verified against the code on `main`; not run end to end on hardware yet.

**Works only on the stage stack with a real controller on the python HID
adapter.** The `unstable` adapter is registered only when a python adapter is
loaded (`services/controller_manager/backend_factory.py`), and mock-only
controllers skip rollout routing (`multiplexer/multiplexer_backend.py`). On the
mock stack the agent only ever shows `expand`. A rollback needs real
degradation. Since #1371 the baseline `controller.json` default backend is
`rust`, which loads no python adapter; `backend` is not runtime-mutable, so this
beat needs a baseline with a python variant (`python_rust`, a reviewed edit of
`services/flagd/controller.json` plus `scripts/flags.sh restart`, then
`talk-stage.sh reset`) and the `python-hid` compose profile (add
`--profile python-hid` to the section 1 bring-up, or use the systemd drop-in
described in `scripts/setup/joustmania.service`; never put `COMPOSE_PROFILES`
in the tracked `.env`). The rollout routes its serials itself; outside this
beat, real controllers reach python-hid only with `bluetooth_backend=python`
as well (#1402).

What happens: the agent watches the `controller.bluetooth_health` span (~1 Hz)
and checks it against the `fitness.bluetooth.*` thresholds (gap 50 ms, drops 2%,
rate 10 Hz). The `unstable` adapter delivers fresh frames at most every 120 ms
(below 10 Hz, e.g. ~7.5 Hz observed) and drops every 10th one, so all three
checks fail.

```bash
setflag agent rollout_enabled on            # agent may move the rollout stage (else dry-run)
setflag rollout remediation_allowed on      # agent may roll back (else recommended_only)
setflag rollout target_backend unstable
setflag rollout strategy progressive        # last: this starts the rollout
```

Expected in Jaeger (`agent.infrastructure.decision`):

1. `remediation.action=expand`, `rollout.controller_count=1`,
   `rollout.dry_run=false`, `fitness.passing=true`. The flag service now serves
   `current_controller_count: one` (`./scripts/flags.sh get rollout current_controller_count`).
2. Within a few seconds the first controller (by sorted serial) is routed to
   `unstable`: `fitness.passing=false`,
   `fitness.violations="movement_update_hz[<serial>] 7.5<10; ..."` (example),
   `remediation.action=rollback`, count back to `none`.
3. `none` during the 30 s cooldown (`AGENT_ROLLOUT_COOLDOWN_SECONDS`), then
   expand again, fail again: a ~30 s cycle, not a climb to three.

Variant for the permissions story: with `remediation_allowed=off` the span says
`remediation.action=recommended_only` and nothing is written.

A dry run (`rollout_enabled` off) keeps no rollout state, so flipping it back
on resumes without a restart.

Reset after a run (no agent restart: once the agent sees the rollout inactive, or
sees a `current_controller_count` it did not write, it drops its last written stage;
#1384). The post-rollback cooldown is kept, so after a reset that follows a
rollback the next run's first expansion can wait up to the cooldown (about 30 s,
`AGENT_ROLLOUT_COOLDOWN_SECONDS`):

```bash
setflag rollout strategy off; setflag rollout target_backend python
setflag rollout current_controller_count none; setflag rollout remediation_allowed off
setflag agent rollout_enabled off
```

The infra loop is not gated by the `enabled` kill switch, only by
`rollout_enabled` (see beat 5).

---

## 5. Pre-flight checklist

### T-24h

- [ ] Pi on the talk pin: `./scripts/setup/joustmania-upgrade.sh <joustmania-vX.Y.Z | sha>`
      pulls that pin's images before it switches; `./scripts/setup/joustmania-upgrade.sh --status`
      shows the pinned image tag (no WARN). Run on the Pi itself: arm64 images,
      **verify in rehearsal**.
- [ ] The running agent has its backend keys (values never printed; also
      section 4 of `./scripts/talk-preflight.sh`):
      ```bash
      for k in JEV_API_KEY ANTHROPIC_API_KEY DECISION_OPENAI_API_KEY; do
        docker exec joustmania-agent sh -c "test -n \"\$$k\"" && echo "$k set" || echo "$k MISSING"
      done
      ```
      `JEV_API_KEY` and `ANTHROPIC_API_KEY` must be set; `DECISION_OPENAI_API_KEY`
      only if beat 6b stays in.
- [ ] Reboot test (#1488): `sudo systemctl restart joustmania`, wait for it,
      then `docker inspect -f '{{.Config.Image}}' joustmania-agent` ends in the
      tag derived from the pinned checkout (not `0.7.0`), the journal shows
      `Env files: .env + .env.local`, and the key check above still says `set`.
- [ ] No dev ports on the venue network (#1319): `sudo ss -tlnp | grep -v 127.0.0.1`
      lists port 80 (envoy) and no 50052-50062, 8013, 8015, 4317 or 50058.
      Also expected: sshd on :22 and the pairing daemon's 8002 on
      `172.17.0.1` (docker0 gateway, for the connect-proxy; not the venue
      network). Anything else from docker is a finding.
      If 50062 shows up, the stack was started without `joustmania-compose.sh`
      (the dev override got loaded): bring it up again as in section 1.
- [ ] Pairing daemon still reaches rust-hid, the collector and flagd over
      `127.0.0.1` (#1319): a USB pairing works, its spans show up in Jaeger,
      and a runtime pairing flag change applies.
- [ ] No stale `joustmania-python-hid` from an older install (#1402):
      `docker ps -a --filter name=joustmania-python-hid` is empty. The start
      script removes it when the `python-hid` profile is off; by hand:
      `docker rm -f joustmania-python-hid`.
- [ ] `.env.local` holds `JEV_API_KEY`, `ANTHROPIC_API_KEY`,
      no `IMAGE_TAG` (the pin is the checked-out
      commit); `DECISION_OPENAI_API_KEY` only if beat 6b stays in. Nothing in
      `.env` (`git diff .env` is empty).
- [ ] Both keys have budget/credit. In `services/agent`:
      Jev: `JEV_API_KEY=... go test -tags live ./decide/backends/jev -run 'TestLiveGate$' -v`
      (4 gate calls; logs p_yes and the pick per calibration state);
      Claude: `ANTHROPIC_API_KEY=... go test -tags live ./decide/backends/claude -run TestLiveDecide -v`
      (or a warm-up game's summary with `llm.retro.parse_ok=true`).
      Both tests SKIP and still print `ok` when the key is empty or mistyped:
      look for `--- PASS` (not `--- SKIP`) in the `-v` output.
- [ ] Vendor variants added to `decision.openai.*`, if beat 6b stays in.
- [ ] `fake-hwclock` installed and enabled on the Pi (`sudo apt install
      fake-hwclock`): it restores the last saved time at boot. Without an RTC
      battery the Pi keeps no time while powered off.
- [ ] Full run-through of all beats on the demo hardware, with a stopwatch:
      fill in the "measured" column of section 9, including how long the
      post-game summary takes.
- [ ] Capture fallback material: screenshots of each beat and exported Jaeger
      traces for beats 1, 2 (one act, one noop), 3a (held), 5, 7 and 8 (retro).
- [ ] Controllers charged; spares charged.

### T-1h (on site)

- [ ] Venue network plan: venue WiFi if it allows outbound HTTPS **and** NTP;
      otherwise the phone hotspot for the Pi. The Pi and the presenter laptop
      must be on the same network.
- [ ] Clock: `timedatectl` shows `System clock synchronized: yes`. If not:
      move the Pi to the hotspot (NTP over the phone), or set it by hand
      (`sudo date -s "<time from phone>"`). A wrong clock breaks Jaeger UI
      lookback searches (the pre-filled `lookback=15m` tabs), Grafana panels
      and the dashboard's Loki activity feed. The Decisions panel itself does
      not depend on the clock.
- [ ] Stack up (section 1). `docker compose ps`: all healthy.
- [ ] Apply the stage baseline (section 6).
- [ ] Controllers paired and READY (bright LED).
- [ ] Warm-up games: one full game on `jev`, one on `claude`, so the first
      on-stage calls are not cold connections. After each game check the
      summary: the Jaeger retro tab shows `agent.llm.retro` with
      `inference.used=claude` and `llm.retro.parse_ok=true` (only that span
      proves success: the log `agent.llm.retro_captured` is written before
      the model call).
- [ ] Back to baseline, then `./scripts/talk-preflight.sh`: no FAIL, and no
      WARN on the budget, and `inference.backend = claude` passes.

### T-5min

- [ ] `./scripts/talk-preflight.sh` again (no FAIL; the Jaeger check needs a
      decision from the last 15 minutes).
- [ ] `./scripts/agent-backend.sh status` shows backend `rules`, threshold
      `default`, jev thr. `gate`, `capture on`.
- [ ] Browser tabs open, in order: dashboard Agent tab, Jaeger agent.decide
      search (pre-filled), Jaeger held search, Jaeger retro search, Grafana
      stage view `http://localhost/grafana/d/talk?kiosk` (optional), terminal.
- [ ] Terminal: an ssh session on the Pi, big font, `cd` to the repo,
      `setflag` pasted,
      history cleared of anything with a key.
- [ ] Notifications off on laptop and phone; phone hotspot on standby.

Stage values: `decision.backend=rules` (the session start; beat 2 flips to
`jev`), `decision.jev.act_confidence_threshold=gate` (0.6, Jev's calibrated
bar), `decision.act_confidence_threshold=default` (0.8, claude/openai),
`decision.min_interval_ms=default` (3000 ms), `decision.claude.timeout_ms=patient`
(8000 ms, #2123: covers cold Claude calls), `decision.jev.timeout_ms=default`
(2500 ms), `llm.max_requests_per_minute=stage` (30/min; the flag sets the budget, a set
`AGENT_LLM_MAX_REQUESTS_PER_MINUTE` can only lower it), `decision.capture_content=on`, `enabled=on`, `experiments_enabled=off`,
`interventions_allowed=standard` (all of it: `./scripts/talk-stage.sh reset`).

---

## 6. Reset to baseline (between rehearsals, and at T-1h)

```bash
./scripts/talk-stage.sh reset     # clear every stage overlay, apply the stage baseline again
./scripts/talk-preflight.sh
```

`talk-stage.sh reset` clears every stage overlay (`DELETE /agent?writer=stage`)
and applies `scripts/talk-baseline.json` again through the flag service:
`decision.backend=rules`, both thresholds (`default`,
Jev's `gate`), capture `on`, `min_interval_ms` `default`, claude model `haiku`,
claude timeout `patient`, jev timeout `default`, `llm.eligible_game_kinds=real_only`, `enabled=on`,
`interventions_enabled=on`, **`interventions_allowed=standard`**,
**`policy.max_interventions_per_minute=aggressive`** (4), `experiments_enabled=off`, `rollout_enabled=off`, plus `decision.jev.base_url`,
`decision.openai.base_url` / `.model` / `.reasoning_effort` back to their
shipped variants, `llm.max_requests_per_minute=stage` and
`experiment.mock_archetypes=all` (it includes `calm_long`), `experiment_tick_seconds=default`
and `experiment_dynamic_enabled=off` (this also turns the screen-only fallback off). The
agent's interventions need no reset: they belong to their game and the flag
service drops them when the game ends (UC-053 BR-003). It is idempotent and does not care what a beat
left behind, so it is also the recovery after an interrupted beat. It leaves
`decision.jev.model` alone (`latest` or `pinned` is the presenter's choice).
After beat I also run the rollout reset from section 4.

**Why `standard` and not the shipped `ambient`:** on the mock stack nothing in
`ambient` is visible (a controller effect defaults to rumble, volume 0.7 is
barely audible); `standard` adds `grant_shield` and `adjust_music_tempo`, which
the audience sees on the dashboard tile and hears in the music (#1301). The
stage baseline lives in the helper; the shipped baseline keeps `ambient`. The
host's persisted admin choice of `interventions_allowed` (menu kill switch) is a
layer under the stage overlay: `reset` does not touch it.

**Why `aggressive` (4) and not the shipped 2 per minute (#1326):** the
intervention budget is per game and weighted (nudge 0.5, shield / tempo /
handicap 1). At 2 the beats run back to back about a minute apart and each one
wants a visible act (a shield or a tempo change). Nudges may spend 1.0 of the
2 (the reserve keeps the other 1.0 for a shield or handicap, UC-040 BR-015),
so only one visible act a minute is guaranteed and a beat often lands on a
full budget: its act row shows `rate_limit`. At 4 there is room for a visible
act per beat plus the ambient nudges, and the coordinator backstop (60 per
minute) stays far away. The shipped default stays 2.

`talk-preflight.sh` checks exactly this baseline (`enabled=on`,
`interventions_enabled=on`, `interventions_allowed=standard`,
`policy.max_interventions_per_minute=aggressive`, FAIL naming
an interrupted permissions beat for `none` / `probe`,
`rollout_enabled=off`, `experiments_enabled=off`, `decision.backend=rules`,
both thresholds (`default` and Jev's `gate`), capture `on`, `min_interval_ms`
`default`, claude model `haiku`, `decision.claude.timeout_ms` `patient`,
`decision.jev.timeout_ms` `default`, `llm.eligible_game_kinds=real_only`,
`decision.jev.base_url` not left on `blackhole` and the `blackhole` variant
present; FAIL when Jev's threshold flag lacks the `strict` or `gate` variant of beat 3a, WARN when
`decision.claude.timeout_ms` lacks `patient`). It
checks the budget (the flag's value ≥ 30, i.e. `stage`, and that a set env ceiling is not below 30),
that `JEV_API_KEY` and `ANTHROPIC_API_KEY` are present (presence only) and that
the stage baseline `inference.backend = claude`, and the effective `inference.backend` flag again in the agent section (FAIL otherwise: beat 8 shows a capture-only
retro; there is no env fallback any more, #1907). It FAILs when the resolved allow-list holds no perceptible
intervention (`play_audio_cue`, `grant_shield`, `toughness_boost`,
`adjust_music_tempo`, `ramp_tempo`, `eliminate_player`, `revive_player`,
`end_game`) and when the agent's audio cue (`defaultAudioCue` in
`services/agent/actions/writer.go`, `agent_cue`) does not resolve to a file in
the **running** `joustmania-audio` container (an image from before #1301 has
no `agent_cue` alias). Set `TALK_EXPECT_BACKEND=jev` if you decide to start on Jev. It
checks the flag service (flagd holds its sync stream, the agent set is served)
and reports how many game-scoped intervention values are live. Last, it
counts the real (non-mock) PS Move controllers controller-manager reports
through envoy and FAILs below `TALK_MIN_CONTROLLERS` (default 2), or when
controller-manager does not answer; with the fallback on that is a WARN (see
[Fallback: Bluetooth down](#fallback-bluetooth-down)).

After the talk, return to the shipped, fail-closed defaults: clear the
persisted backend and model, then clear the stage overlays, or restart the flag
service and flagd together (in-memory overlays are gone; the persisted backend
is not, so clear it first; the host's other persisted admin settings stay):

```bash
./scripts/agent-backend.sh rules   # persisted rules; or ./scripts/talk-stage.sh reset
./scripts/flags.sh reset agent stage
./scripts/flags.sh restart
```

Residual risk (C-018): with `enabled` on in the flag file and a remote backend
still persisted, the agent makes remote decisions again after a reboot. The
first step above prevents that; the pre-flight WARN names the persisted backend.

---

## 7. Failure modes and recovery

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| Remote backend selected but no `agent.decide` spans at all, `agent_llm_gated_total{reason=no_interventions_allowed}` rising | `interventions_allowed` is `none` (or `probe`): with nothing allowed the agent makes no remote call (#1298) | `talk-preflight.sh` WARN `allows no intervention` | `./scripts/talk-stage.sh reset` (live; restores `standard`) |
| Green `act` row, but nothing seen or heard in the game | The act was applied but is imperceptible: `interventions_allowed` off the stage baseline (only `ambient`, or `none` left by an interrupted beat), or the audio image predates the `agent_cue` alias | `talk-preflight.sh` (allow-list, perceptible and audio-cue lines). After a beat both `game_interventions_total{blocked="false"}` **and** `audio_sounds_played_total{result="success"}` (cue / shield sound) should rise | `./scripts/talk-stage.sh reset`; for the cue, pull / rebuild the audio image |
| Rows show `~~jev~~ -> rules`, `backend_unavailable` | `JEV_API_KEY` not in the agent container (keys are read at startup) | `talk-preflight.sh` key section | Add the key to `.env.local`, recreate the agent (`... up -d agent`). On stage: flip to the backup, `backend.sh claude` |
| `~~jev~~ -> rules` with `timeout` or `error` on every row | Jev unreachable or slow from the venue (network, TypeSafe outage, 401 / 429 / 5xx; `error.type` says which) | `error.type` and `decide.latency_ms` on the span | **`backend.sh claude`** (the manual backup; there is no automatic jev -> claude chain). Rules already covered every cycle, so the game never stalled: say so. Try `backend.sh jev` again later |
| `~~claude~~ -> rules`, `backend_unavailable` | `ANTHROPIC_API_KEY` missing | `talk-preflight.sh` key section | Add it to `.env.local`, recreate the agent; on stage go back to `backend.sh jev` |
| `fallback_reason=llm_budget_exhausted` after a minute of remote decisions | Global budget spent (decisions + summaries share it); baseline not applied | `talk-preflight.sh` budget line | `setflag agent llm.max_requests_per_minute stage` (live; the flag sets the budget). If `AGENT_LLM_MAX_REQUESTS_PER_MINUTE` is set below 30 it caps the flag: raising it needs an agent recreate, the only live lever is `setflag agent decision.min_interval_ms slow` (10 s cadence) |
| `fallback_reason=timeout` on Claude | Slow venue network or slow model (`decision.claude.timeout_ms` stage baseline `patient` 8000 ms since #2123, shipped default 5000 ms; Haiku warm about 2.7-3.0 s, Sonnet about 2.85 s) | `decide.latency_ms` on earlier rows; `decision.claude.timeout_ms` still at `patient` | `setflag agent decision.claude.timeout_ms patient` if it drifted; stay on haiku; or back to `backend.sh jev` |
| Jev never acts in beat 2 | The game is calm (gate p_yes below 0.6) | `jev.gate.p_yes` on the stage-1 chat span | Push the players to the edge / let eliminations pile up (mock: `demo_driver.py --scenario death_cascade`); if `jev-latest` changed, `backend.sh jev pinned`. Do not lower the bar on stage unless you say why |
| `held` never appears in 3a | No open gate during the beat (calm play gives `noop`, not `held`) | `decide.outcome` on recent rows | Keep players near the threshold; narrate the evaluation event of a captured held trace |
| Beat 8: no `agent.llm.retro` span at all | The budget was full at game end: the retro is gated before any span is created | `docker compose logs agent \| grep agent.llm.retro_gated` (`fallback_reason=llm_budget_exhausted`) | `setflag agent llm.max_requests_per_minute stage` (an env ceiling below 30 must be raised or unset first); show the summary of an earlier game or a captured trace |
| Beat 8: `agent.llm.retro` with `inference.used=none` | `inference.fallback_reason`: `no_backend_available` = `inference.backend` not `claude` (or the key empty); `agent_disabled` = the kill switch was off when the game ended; `llm_not_eligible` = not a real game | the retro span tags; preflight `inference.backend` line | Flag: `./scripts/talk-stage.sh reset` (key missing: fix `.env.local`, recreate the agent, before the talk only). Otherwise show the summary of an earlier game |
| Beat 8: `inference.used=claude` but `llm.retro.parse_ok=false` | The model call failed (timeout, HTTP error, refusal) or the reply did not decode | `llm.infer.error` / `error.type` on the span (status error); log `agent.llm.retro_failed` | Check the uplink (beat 7 option A restored?) and Anthropic credit; show the summary of an earlier game |
| Beat 8: the summary takes too long | The retro model call is slow (default Opus, effort medium) | `llm.retro.latency_ms` from rehearsal | Show the summary of the game that ended earlier; for the next talk set the `inference.model` agent flag to a faster model through the flag service (#1788) (**verify in rehearsal**) |
| Vendor budget / credit exhausted (HTTP 429 / 402) | Account limit | Jaeger: `fallback_reason=error`, `error.type` | Switch to the other backend (`backend.sh jev` / `claude`); the fallback itself is a talking point |
| `fallback_reason=llm_not_eligible` | Game kind is not `real` (a shadow game, or eligibility flag changed) | `game.kind` on the span | Play a menu-started game; `setflag agent llm.eligible_game_kinds real_only` |
| No new rows at all | No game running, kill switch off, or on `rules` and nothing to decide | Is a game running? `agent-killswitch.sh status` | Start a game; `agent-killswitch.sh on` |
| Jaeger tabs / Grafana empty, Decisions panel fine | Pi clock skewed (lookback searches use the clock) | `timedatectl`; preflight clock line | Fix the clock (section 5) |
| `agent-killswitch.sh off` fails (it prints the fallback) | The flag service is down or unreachable, so the `enabled` overlay cannot be cleared | the helper's error lines; `./scripts/flags.sh state` | **`docker stop joustmania-agent`**: the brake without the flag service; the game keeps running without the agent. Later `docker start joustmania-agent` (the restarted agent reads `enabled` again, and a restarted flag service serves the fail-closed `off`) |
| Agent writes refused with `409 game_not_live` right after a flag-service restart | The coordinator has not re-registered its live games yet | `./scripts/flags.sh state` (`live_games`) | Wait: the coordinator sees the new epoch within about 2 s (full reconcile every 15 s) |
| Flag flipped, nothing changed | flagd lost its sync stream to the flag service (writes then fail with `503 no_sync_stream`) | `talk-preflight.sh` "flag service" section; `./scripts/flags.sh state` (`sync_streams`) | `./scripts/flags.sh restart` (flag service, then flagd; about 5 s), then `talk-stage.sh reset` |
| A flip is refused (`422 flag_read_only` / `out_of_bounds`) | The flag is not runtime-mutable, or the variant does not exist | the helper's error line | Use a declared variant (`./scripts/flags.sh get agent <flag>`); read-only flags change only by a reviewed file edit and a flag-service restart |
| Dashboard Decisions panel empty / stale, Jaeger slow | Jaeger query slow on the Pi (whole game traces) | Open the Jaeger search directly | Narrow the Jaeger lookback; restart jaeger as a last resort (`docker compose restart jaeger`); present from the captured screenshots |
| `invalid_answer` rows on the vendor (6b) | Model does not follow the JSON schema / rejects `reasoning_effort` | `error.type`, `decide.invalid_reason` | `backend.sh effort omit`; pick another model; or skip 6b |
| Triggers do not ready controllers, `joustmania-status` shows `service rust-hid: running (unhealthy)` | rust-hid wedged under many controllers (#2125) | `joustmania-status`; `docker logs joustmania-rust-hid \| grep watchdog` | Wait about 30 s: its watchdog exits and compose restarts it (UC-002 A7); if still unhealthy, `docker restart joustmania-rust-hid` (the line `joustmania-status` prints). Controllers reconnect on their own; players press the trigger again |
| Real controllers will not pair or keep dropping (venue Bluetooth) | 2.4 GHz congestion, dongle, batteries | `talk-preflight.sh` real-controller line | Try re-pairing once; then [Fallback: Bluetooth down](#fallback-bluetooth-down) |
| Fallback on, screen empty right after the kill-switch beat | `enabled=off` aborted every experiment and force-ended its shadow games | `talk-stage.sh status`; Experiments tab | `agent-killswitch.sh on`; the next tick (about 10 s with the fallback) declares a new experiment. See [Fallback: Bluetooth down](#fallback-bluetooth-down) |
| Whole demo wedged | anything | `docker compose ps` | `backend.sh rules`: rules need no network and no key. Last resort: screenshots and captured traces |


### Fallback: Bluetooth down

Insurance for the venue (UC-070 A9, #1316): if the real controllers do not
pair, the talk runs screen-only on the experiment loop's archetype shadow
games (pattern-driven mock controllers, UC-046). The agent decides on them
like on a real game, so every beat from 2 to 7 still shows.

```bash
./scripts/talk-stage.sh fallback on    # llm.eligible_game_kinds=real_and_shadow, experiments_enabled=on,
                                       # experiment_dynamic_enabled=on, experiment_tick_seconds=fast
                                       # (flag service overlays, live at once)
./scripts/talk-preflight.sh            # real-controller check now WARNs; fallback variants accepted
# ... beats 2-7 as usual (agent-backend.sh jev, threshold, claude, kill switch, ...)
./scripts/talk-stage.sh fallback off   # back to real_only, experiments off, tick default (reset does this too)
```

**Where the games come from.** `experiments_enabled=on` starts the experiment
loop; `experiment_dynamic_enabled=on` lets the agent declare experiments
itself (on game-tuning flags of the `game` set only, the experiment catalog: plumbing, lobby and ceremony
flags are excluded, UC-070 BR-008; shadow games only, promotion stays gated), so no
env change or agent restart is needed. An `experiment_seed` variant (what
`agent-dryrun-enable.sh on` sets) also works, but a seed left set is not declared
again: after the kill-switch beat (`enabled=off` aborts every experiment) or a
`fallback off`, only the dynamic declarer brings games back, unless the seed is set
to `off` for one `experiment_tick_seconds` and then set again
(`agent-dryrun-enable.sh off`, then `on`).
The loop refills on each experiment tick: `fallback on` selects the `fast`
variant of `experiment_tick_seconds` (10 s instead of 30 s); `fallback off`
and `reset` restore `default`. The controller-manager always loads the mock adapter, so pattern
controllers work on the stage stack next to the real ones. **Verify in
rehearsal** how long the first shadow game takes to appear.

**A game is always in progress.** The archetype selection
`experiment.mock_archetypes` (stage: `all`) includes `calm_long`: it kills
nobody and runs until `experiment.max_game_seconds` (300 s), while up to four
shadow games run at once. `fallback on` and the pre-flight warn when the
selection has no `calm_long`; `reset` restores `all`.

**What the audience sees.** With no real game live the dashboard follows the
newest live shadow game and stays on it while it runs:

- Agent tab: the current-backend strip describes that game, with the dashed
  `shadow` badge; decide rows of the shadow games carry the same badge.
- Controllers tab: a "following shadow game" banner and that game's tiles
  added to the grid: live LED colour, the shield badge when the agent shields
  a player, alive or dead. Players of the other shadow games are hidden; real
  and lobby controllers always stay. The lobby and the menu never show the
  shadow controllers (pattern controllers are reserved).
- Jaeger: `agent.decide` spans with `game.kind=shadow`.

**Warning: the kill-switch beat empties the screen.** `agent-killswitch.sh off`
aborts every experiment and force-ends its shadow games. Nothing is followed
until you switch the agent back on and the next tick (about 10 s) has declared
an experiment and spawned its games. Narrate the gap ("the agent is off, so
its games are gone too") or skip beat 5 in the fallback.

Say it: "Bluetooth is not on our side tonight, so you are watching the agent
steer the games it runs on its own. Same flags, same traces."

**Cost.** While the fallback is on, Jev / Claude also decide on shadow games
(up to four at once). All remote calls share the one per-minute budget,
`llm.max_requests_per_minute` (`stage` 30/min; a set env ceiling can only lower it), so the
cost is bounded by it; once it is spent, decisions fall back to rules with
`llm_budget_exhausted`. Turn the fallback off as soon as real controllers are
back, and always after the talk (`reset` also does it).

---

## 8. What the audience should take away

| Beat | OpenTelemetry gives | OpenFeature gives |
|---|---|---|
| 1 Observe | The decision, its reason and its confidence as a standard GenAI span (`gen_ai.*` + `agent.decide`), in any OTel backend | (the flags in effect are part of the decision record) |
| 2 / 4 Model switch | `gen_ai.request.model` / `gen_ai.provider.name` prove which model decided, per decision; Jev's gate `p_yes` explains act vs noop | One flag (`decision.backend`) picks the backend, live, no redeploy; the backup is one flip away |
| 3 / 5 Permissions | `held`, `noop`, `discarded` are visible with the reason; the evaluation event names the threshold flag | Per-backend thresholds, the allow-list (which shapes the question itself) and the kill switch are flags, evaluated at use time |
| 6 Rollback | The trace timeline shows before/after; infra: `remediation.action` tells the story | Rollback = flip the flag back. Infra: the agent itself rolls back by writing a flag |
| 7 Degrade | `decide.fallback_reason` + `error.type` make the degradation explicit | Per-backend timeouts and base URLs are flags; rules is the floor, the game never waits |
| 8 Summary | The post-game summary and its suggestions are a span linked to the game trace | The kill switch and the shared budget govern the post-game model calls too |

One line: **OTel is how we see what the agent did and why; OpenFeature is how we
change what it is allowed to do, while it runs. Both are open standards; no
vendor control plane.**

---

## 9. Rehearsal record (fill in on the demo hardware)

Reference values from development machines (not the Pi): Jev noop round
0.22-0.34 s, act round 0.47-0.57 s; Claude Haiku 2.7-3.0 s and Sonnet about
2.85 s warm ([decide contract §8](../specs/decide-contract.md)).

| Beat | Measured: flip -> first new row | Measured: `decide.latency_ms` p50 | Notes |
|---|---|---|---|
| 1 rules decisions per minute (4 players) | | n/a | |
| 2 jev noop round | | | |
| 2 jev act round (how to provoke it reliably) | | | |
| 3a held on `jev-threshold strict` | | | |
| 4 claude haiku | | | |
| 5 discard caught? (y/n, tries) | | | |
| 6b vendor | | | |
| 7 fallback (network off / blackhole) | | | |
| I expand -> rollback | | n/a | |
| 8 game end -> retro span visible | | n/a (`llm.retro.latency_ms`) | |
