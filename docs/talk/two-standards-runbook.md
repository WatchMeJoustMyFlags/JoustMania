# Talk runbook: "Two Standards Walk Into an Agent"

**Runtime Agent Control with OpenTelemetry and OpenFeature.** The operator
script for the live demo (issue #743, epic #1225).

> Autonomous agents are being deployed without runtime control. No open,
> vendor-neutral way to change what they do. No visibility into why they
> decided. We can fix that.

The demo is a physical party game (JoustMania, PS Move controllers) on a
Raspberry Pi 5. An agent watches the game only through OpenTelemetry and is
steered only through OpenFeature flags served by flagd. On stage we show:
observing a decision, switching the model live, tightening permissions, rolling
back without a redeploy, and degrading to rules when the network goes away.
The experiments / shadow-games / dark-factory material from
[NORTHSTAR.md](../NORTHSTAR.md) is **not** in this talk.

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

The helper scripts edit **one** flag file, `$AGENT_FLAG_FILE`. It must be the
file flagd serves. `./scripts/talk-preflight.sh` checks this (the
"flag dir" section).

| | Stage stack (Pi, real controllers) | Rehearsal / fallback stack (mock controllers) |
|---|---|---|
| Bring-up | `docker compose --env-file .env --env-file .env.local up -d` | `COMPOSE_ENV_FILES=.env,.env.local make dry-run` |
| Compose files | `docker-compose.yml` + the auto-loaded `docker-compose.override.yml` | `docker-compose.yml` + `override` + `ci` + `dry-run`, profiles `agent` + `dashboard` |
| Flag dir flagd serves | `services/flagd/` | `services/flagd/ci/` |
| `AGENT_FLAG_FILE` | `export AGENT_FLAG_FILE=$PWD/services/flagd/agent.json` | unset (helpers default to `services/flagd/ci/agent.json`) |
| Gameplay | real players | `tools/demo/demo_driver.py` (scripted mock movement) |
| Infra rollback beat | possible (needs the python HID adapter) | not possible (see beat 4b) |

Notes:

- On the stage stack the helpers write the tracked `services/flagd/agent.json`.
  The agent also writes `interventions.json` (and `rollout.json` in beat 4b)
  in the same dir. That is fine for a talk machine; restore all three
  afterwards (section 6).
- `make dry-run` also sets the experiment env vars, but the live gate
  `experiments_enabled` stays `off`, so the experiment loop is inert. Keep it
  off.
- `COMPOSE_ENV_FILES` needs a Compose version that supports it; if in doubt use
  the explicit form from the `make dry-run` recipe with
  `--env-file .env --env-file .env.local` added. **Verify in rehearsal.**

### Keys and env: `.env.local`, never `.env`

`.env` is tracked in git and pins `IMAGE_TAG=0.7.0`, which predates the
decision backends. Put everything below in the git-ignored `.env.local`
(later `--env-file` wins). Use `latest` while rehearsing, then **pin the exact
tag you rehearsed with** (a release or `sha-...` tag) for the talk, so a new
push to `main` cannot change the stage images:

```bash
# .env.local  (git-ignored; never commit, never paste on screen)
IMAGE_TAG=<the tag you rehearsed with>  # latest while rehearsing; must contain #1246/#1247
ANTHROPIC_API_KEY=...                 # claude backend
DECISION_OPENAI_API_KEY=...           # second vendor (OpenAI-compatible)
# JEV_API_KEY=...                     # leave unset while Jev is a stub
AGENT_LLM_MAX_REQUESTS_PER_MINUTE=30  # see "budget" below
```

The agent reads all of these **once at startup**. Changing one means
recreating the agent:
`docker compose --env-file .env --env-file .env.local up -d agent` (add the
dry-run `-f` files and profiles on the rehearsal stack).

**Budget.** Remote decide calls share one global per-minute budget, and the
post-game Claude retro draws from the same budget. The cap is the
`llm.max_requests_per_minute` flag (default `6`, `aggressive` is `12`; #1231 is
adding a `stage` variant), **unless** `AGENT_LLM_MAX_REQUESTS_PER_MINUTE` is set:
then the env value wins and the flag is ignored
(`budgetCap`, `services/agent/decision/decision.go`). One game at
`decision.min_interval_ms=3000` asks up to 20 times a minute, so the default
budget runs out after six calls and the rest fall back to rules with
`decide.fallback_reason=llm_budget_exhausted`. Set the env override to 30
(headroom for retros, and a second game).

### The second vendor (placeholder: fill in before T-24h)

`decision.openai.model` ships with only an empty `unset` variant and
`decision.openai.base_url` with only `default` (`https://api.openai.com/v1`).
Add one variant to each in `$AGENT_FLAG_FILE` for your vendor (by hand, then
check with `python3 -m json.tool "$AF" >/dev/null`):

```json
"decision.openai.model":    { "variants": { "unset": "", "vendor": "<VENDOR_MODEL_ID>" }, ... },
"decision.openai.base_url": { "variants": { "default": "https://api.openai.com/v1",
                                            "vendor": "<VENDOR_BASE_URL>/v1",
                                            "blackhole": "http://10.255.255.1/v1" }, ... }
```

- `gen_ai.provider.name` is derived from the base URL host
  (`services/agent/decide/backends/openai/provider.go`): the semconv
  well-known value for OpenAI, Gemini, Vertex AI, Mistral, Groq, DeepSeek, xAI,
  Perplexity, Cohere, Azure, Bedrock and IBM watsonx hosts. Unknown hosts
  (self-hosted, other vendors) record `openai`.
  Pick a vendor whose host is in that list if you want the provider name to
  change visibly on stage.
- `blackhole` is a non-routable address for the degrade beat (beat 5, option B).
- If the model rejects `reasoning_effort=low`, run
  `./scripts/agent-backend.sh effort omit`.

### A helper for flags that `agent-backend.sh` does not cover

`agent-backend.sh` flips the `decision.*` flags and `agent-killswitch.sh` flips
`enabled`. For the rest (`interventions_allowed`, `interventions_enabled`,
`decision.min_interval_ms`, `decision.openai.base_url`, rollout flags) paste
this function into the presenter shell:

```bash
# setflag <file> <flag-key> <variant>: set defaultVariant, refuse unknown flags/variants
setflag() {
  python3 - "$@" <<'PY'
import json, sys
if len(sys.argv) != 4:
    sys.exit("usage: setflag <file> <flag-key> <variant>")
path, key, variant = sys.argv[1:4]
with open(path) as fh:
    data = json.load(fh)
flag = data.get("flags", {}).get(key)
if flag is None:
    sys.exit(f"flag {key!r} not in {path}")
if variant not in flag.get("variants", {}):
    sys.exit(f"{variant!r} not in {list(flag.get('variants', {}))}")
flag["defaultVariant"] = variant
with open(path, "w") as fh:  # in place, like the helper scripts
    fh.write(json.dumps(data, indent=2) + "\n")
print(f"{key} -> {variant}  ({path})")
PY
}
AF="${AGENT_FLAG_FILE:-services/flagd/ci/agent.json}"
RF="$(dirname "$AF")/rollout.json"
```

flagd notices the file change and serves the new value in about a second; the
agent reads every flag used below at use time, so **no restart** for any flag
flip. All commands run in an **ssh session on the Pi**, in the repo root
(the helpers edit files on the Pi). Always quote `"$AF"` / `"$RF"`.

**Show the flag change, not only its effect.** The OpenFeature half of the talk
is only visible if the audience sees the flag move. Keep the terminal next to
the dashboard and, after each flip, run `./scripts/agent-backend.sh status` (or
`git diff --word-diff "$AF"` on the stage stack, where the file is tracked).

### URLs (through envoy on port 80)

Replace `localhost` with the Pi's address from the presenter laptop.

| Tab | URL |
|---|---|
| Dashboard, **Agent** tab, **Decisions** panel (#1246) | `http://localhost/` |
| Jaeger, agent.decide search | `http://localhost/jaeger/search?service=agent&operation=agent.decide&lookback=15m&limit=20` |
| Jaeger, held decisions only | `...&tags=%7B%22decide.outcome%22%3A%22held%22%7D` |
| Jaeger, infra decisions (beat 4b) | `http://localhost/jaeger/search?service=agent&operation=agent.infrastructure.decision&lookback=15m` |
| Grafana | `http://localhost/grafana/` (Agent Operations, Fleet) |

The Jaeger search URL parameters are standard Jaeger UI query parameters;
**verify in rehearsal** that the pre-filled search opens directly.

### What is on screen

- **Decisions panel** (dashboard, Agent tab): polls Jaeger every 1.5 s for the
  newest `agent.decide` spans. The query never uses the browser clock (it relies
  on Jaeger's own "now"), so a skewed presenter laptop does not empty it. The
  current-backend strip shows backend, model and act threshold, taken from the
  newest `agent.decide` span, and flashes when one of them changes. On `rules`
  it only updates when rules decide something, so a flip *to* rules shows on
  the next rules decision, not instantly. One
  row per decision: outcome badge (act green, held amber, noop grey, invalid
  purple, fallback orange, discarded dashed), backend (struck through with
  `-> rules` and the reason on a fallback), model, answer, latency, `trace` link.
- **`agent.decide` span** (Jaeger, service `agent`): `gen_ai.agent.name`,
  `gen_ai.conversation.id` (= the game), `gen_ai.request.model`,
  `decide.backend` / `decide.backend.requested`, `decide.fallback_reason`,
  `decide.outcome`, `decide.discarded_reason`, `decide.confidence`,
  `decide.act_confidence_threshold`, `decide.rationale` (the "why"),
  `decide.answers`, `decide.latency_ms`, `error.type`, and a
  `gen_ai.evaluation.result` event ("confidence 0.830 >= threshold 0.800").
- **`chat {model}` CLIENT span** (child of `agent.decide`, remote backends
  only): `gen_ai.provider.name` (`anthropic`, the vendor, or `jev`), request and
  response model, token usage, and with `decision.capture_content=on` the
  prompt and output messages.

How decisions are produced: on `decision.backend=rules` an `agent.decide` span
appears only on cycles where the rules engine decides something. On a remote
backend the agent asks at most every `decision.min_interval_ms` per game, only
for **real** games (`llm.eligible_game_kinds=real_only`), and only while a game
is running and sending telemetry. **No game running = no new rows.** Keep a game
going during every beat.

---

## 2. Talk timing (30-minute slot, about 14 minutes live)

Beats marked **cut** can be dropped first; the order of cutting is 4b, 3c, 6,
2c, 3b. With all of them cut the live part is about 8 minutes (keep a 30-second
plain kill switch when 3c goes).

| Time | Segment | Live? | Cuttable |
|---|---|---|---|
| 0:00 | Problem: agents ship without runtime control (abstract) | no | |
| 3:00 | The game and the architecture: OTel in, OpenFeature out, no control plane | no | shorten |
| 6:00 | Beat 1: observe a decision | yes, 2 min | |
| 8:00 | Beat 2a/2b: live model switch, haiku then sonnet | yes, 2 min | |
| 10:00 | Beat 2c: second vendor | yes, 1 min | **cut** |
| 11:00 | Beat 3a: confidence threshold, held | yes, 1.5 min | |
| 12:30 | Beat 3b: interventions_allowed ladder | yes, 1.5 min | **cut** |
| 14:00 | Beat 3c: kill switch with an in-flight discard | yes, 1 min | **cut** (keep a plain kill switch) |
| 15:00 | Beat 4a: rollback by flipping back | yes, 1 min | |
| 16:00 | Beat 4b: infra rollout, fitness violation, auto-rollback | yes, 2 min | **cut** (hardware only) |
| 18:00 | Beat 5: degrade, never stall | yes, 1.5 min | |
| 19:30 | Beat 6: Jev slot | yes, 0.5 min | **cut** |
| 20:00 | Takeaways: what OTel gave us, what OpenFeature gave us | no | |
| 25:00 | Q&A (buffer: overruns eat this first) | | |

---

## 3. Beat table

Start state for every beat: the stage baseline (section 6), a real game running.
`backend.sh` below is `./scripts/agent-backend.sh`.

| # | Talk beat | Command | Flag changed | Expected on screen (dashboard / Jaeger / game) | Expected latency | Fallback if it doesn't show |
|---|---|---|---|---|---|---|
| 1 | **Observe**: the agent decides; why + confidence | (none; play) `backend.sh status` on screen | none | Dashboard: rows with backend `rules`, outcome `act`. Jaeger: open a row's `trace` link; `agent.decide` with `decide.rationale`, `decide.confidence`, `gen_ai.agent.name`; on the rules path the `agent.decision` spans (action, allowed/blocked) are its **siblings** under `agent.signal_received` (on a remote backend they are its children) | a row per rules decision; rules decisions depend on play. **Verify in rehearsal** how often a 4-player game decides | Run `demo_driver.py --scenario statue` (mock stack) or ask a player to freeze; show a pre-captured trace |
| 2a | **Live model switch** to Claude (haiku) | `backend.sh claude haiku` | `decision.backend=claude`, `decision.claude.model=haiku` | Strip flashes to `claude` / `claude-haiku-4-5-20251001`. New rows backend `claude`. Jaeger: child `chat claude-haiku-4-5-20251001` with `gen_ai.provider.name=anthropic` | flagd reload about 1 s + next decide slot (up to 3 s) + call (about 1-3 s): **verify in rehearsal** | Row shows `~~claude~~ -> rules, backend_unavailable`: key missing, see failure modes |
| 2b | Bigger model, same vendor | `backend.sh claude sonnet` | `decision.claude.model=sonnet` | Model column `claude-sonnet-5-5`; `decide.latency_ms` usually higher | as 2a; **measure** sonnet latency | If sonnet times out (`fallback_reason=timeout`; budget = the current `decision.claude.timeout_ms` default, being retuned in #1231) set `setflag "$AF" decision.claude.timeout_ms patient` before the talk |
| 2c | Second vendor, same contract | First `setflag "$AF" decision.openai.base_url vendor`, **then** `backend.sh openai vendor` (model variant you added), so no call goes to the wrong host | `decision.backend=openai`, `decision.openai.model`, `decision.openai.base_url` | Backend `openai`, model `<VENDOR_MODEL_ID>`. Jaeger `chat <model>` span: `gen_ai.provider.name` changes from `anthropic` to the vendor's value | as 2a; **measure** | `backend_unavailable`: model/base URL variant or `DECISION_OPENAI_API_KEY` missing. `invalid_answer` or `error`: try `backend.sh effort omit`. Else skip back to 2b |
| 3a | **Permissions: confidence threshold** | On claude/openai: `backend.sh threshold strict`; on jev: `backend.sh jev-threshold strict` (Jev has its own bar, #1287) | claude/openai: `decision.act_confidence_threshold=strict` (0.95); jev: `decision.jev.act_confidence_threshold=strict` (0.95) | Rows turn amber `held`; Jaeger event `gen_ai.evaluation.result` "confidence 0.870 < threshold 0.950 (decision.act_confidence_threshold)" (jev: "... (decision.jev.act_confidence_threshold)"), attribute `decide.act_confidence_threshold.source` = `global` / `jev`; nothing acts in the game | next remote decision (about 1-6 s) | `held` exists only on a remote backend (rules does not route by confidence). The global flag does NOT move Jev's bar (unless Jev's is `inherit`). If every answer is above 0.95, nothing changes: show the evaluation event instead. Back: `backend.sh threshold default` / `backend.sh jev-threshold gate` |
| 3b | Permissions: what it may do | On a remote backend: `setflag "$AF" interventions_allowed none`, later `... ambient` | `interventions_allowed` | Rows turn grey `noop`: **the permission shrinks the question the model is asked.** The `ffa.intervention` options are `noop` plus the allowed list (`decide.FFAQuestions`), so with `none` the model can only answer `noop` (visible in `decide.questions`). Back on `ambient` it is offered audio cue / controller effect / volume again and LED / sound nudges apply. (On `rules` you would instead see `act` with the linked `agent.decision` **blocked**, `decision.block_reason=not_allowed`.) | about 1 s + next decision | If nothing ever acts on `ambient`: is `interventions_enabled` on? Try `standard` |
| 3c | **Kill switch**, including a discarded in-flight answer | `./scripts/agent-killswitch.sh off` right after a new row starts | `enabled=off` | No new `agent.decide` rows; Jaeger shows throttled `agent.disabled` spans. If a call was in flight: a dashed `discarded` row with `kill_switch` (`decide.outcome=discarded`, `decide.discarded_reason=kill_switch`). Game keeps playing, agent silent | about 1 s | Catching an in-flight call is timing luck. Make it likelier with `backend.sh claude opus` (slower) **plus** `setflag "$AF" decision.claude.timeout_ms patient` (otherwise opus mostly times out into a fallback). **Verify in rehearsal**. Back: `agent-killswitch.sh on`, `backend.sh claude haiku`, `setflag "$AF" decision.claude.timeout_ms default` |
| 4a | **Rollback without redeploy** | `backend.sh rules` | `decision.backend=rules` | Strip back to `rules`; the next remote answer that lands after the flip, if any, shows `discarded` / `backend_changed`. No container restarted (`docker ps` uptime) | about 1 s | None needed: rules is in-process and cannot be unavailable |
| 4b | Infra: rollout -> fitness violation -> auto-rollback (**cut**, hardware only) | see section 4 | `rollout_enabled`, `remediation_allowed`, `target_backend`, `strategy` | `agent.infrastructure.decision`: `remediation.action=expand` then `rollback`, `fitness.violations` populated, `rollout.dry_run=false` | expand on the first passing health cycle (about 1 s), rollback a few seconds later; **verify in rehearsal** | Skip; show `TestInfraNarrative_FullIncident` or captured spans |
| 5 | **Degrade, never stall** | On a remote backend. Option A (on `claude`): switch off the Pi's uplink (hotspot off / unplug WAN). Option B: `backend.sh openai vendor`, then `setflag "$AF" decision.openai.base_url blackhole` | none (A) or `decision.openai.base_url` (B) | A: rows `~~claude~~ -> rules`, `fallback_reason=error` (DNS / connect failure; fast, no retries). B: rows `~~openai~~ -> rules`, `fallback_reason=timeout` once the timeout expires. Both: `error.type` set, span status error. Game keeps playing; rules keep deciding | A: about a second. B: the current `decision.openai.timeout_ms` default (`fast` = 1500 ms) | If the network drop also kills the presenter's view of the Pi, use option B (local network stays up). Restore network or `base_url vendor` afterwards |
| 6 | **Jev slot** (**cut** while stub) | `backend.sh jev` | `decision.backend=jev` | Today: `~~jev~~ -> rules, backend_unavailable` (no `JEV_API_KEY`, no `decision.jev.base_url`). Then `backend.sh claude` to show the switch away | about 1 s | This *is* the expected stub behaviour; use it as a second degrade example |

---

## 4. Beat 4b: infra rollout and auto-rollback (optional, hardware only)

Verified against the code on `main`; not run end to end on hardware yet.

**Works only on the stage stack with a real controller on the python HID
adapter.** The `unstable` adapter is registered only when a python adapter is
loaded (`services/controller_manager/backend_factory.py`), and mock-only
controllers skip rollout routing (`multiplexer/multiplexer_backend.py`). On the
mock stack the agent only ever shows `expand`. A rollback needs real
degradation. The base `controller.json` default backend is `python_rust`, which
includes python.

What happens: the agent watches the `controller.bluetooth_health` span (~1 Hz)
and checks it against the `fitness.bluetooth.*` thresholds (gap 50 ms, drops 2%,
rate 10 Hz). The `unstable` adapter delivers fresh frames at most every 120 ms
(below 10 Hz, e.g. ~7.5 Hz observed) and drops every 10th one, so all three
checks fail.

```bash
setflag "$AF" rollout_enabled on          # agent may write rollout.json (else dry-run)
setflag "$RF" remediation_allowed on      # agent may roll back (else recommended_only)
setflag "$RF" target_backend unstable
setflag "$RF" strategy progressive        # last: this starts the rollout
```

Expected in Jaeger (`agent.infrastructure.decision`):

1. `remediation.action=expand`, `rollout.controller_count=1`,
   `rollout.dry_run=false`, `fitness.passing=true`. `rollout.json` now has
   `current_controller_count: one`.
2. Within a few seconds the first controller (by sorted serial) is routed to
   `unstable`: `fitness.passing=false`,
   `fitness.violations="movement_update_hz[<serial>] 7.5<10; ..."` (example),
   `remediation.action=rollback`, count back to `none`.
3. `none` during the 30 s cooldown (`AGENT_ROLLOUT_COOLDOWN_SECONDS`), then
   expand again, fail again: a ~30 s cycle, not a climb to three.

Variant for the permissions story: with `remediation_allowed=off` the span says
`remediation.action=recommended_only` and nothing is written.

Reset (and restart the agent: the cooldown and the last written stage live in
memory, and a stale `lastWritten` can block the next first expansion):

```bash
setflag "$RF" strategy off; setflag "$RF" target_backend python
setflag "$RF" current_controller_count none; setflag "$RF" remediation_allowed off
setflag "$AF" rollout_enabled off
docker compose --env-file .env --env-file .env.local restart agent
```

The infra loop is not gated by the `enabled` kill switch, only by
`rollout_enabled`. That does not matter for the order (the kill switch is back
`on` after 3c), but it is worth one sentence on stage: each loop has its own
flag, so you can stop the game agent without stopping infra remediation.

---

## 5. Pre-flight checklist

### T-24h

- [ ] Pi updated, images pulled with the tag from `.env.local`
      (`docker compose --env-file .env --env-file .env.local pull`). Run on the
      Pi itself: arm64 images, **verify in rehearsal**.
- [ ] `.env.local` holds `ANTHROPIC_API_KEY`, `DECISION_OPENAI_API_KEY`,
      `IMAGE_TAG` (pinned to the rehearsed tag), `AGENT_LLM_MAX_REQUESTS_PER_MINUTE=30`. Nothing in `.env`
      (`git diff .env` is empty).
- [ ] Both keys have budget/credit; the vendor model id works (one test call
      from the laptop).
- [ ] Vendor variants added to `decision.openai.model` / `decision.openai.base_url`
      (plus `blackhole`) in `$AGENT_FLAG_FILE`.
- [ ] `fake-hwclock` installed and enabled on the Pi (`sudo apt install
      fake-hwclock`): it restores the last saved time at boot. Without an RTC
      battery the Pi keeps no time while powered off.
- [ ] Full run-through of all beats on the demo hardware, with a stopwatch:
      fill in the "measured" column of section 9.
- [ ] Capture fallback material: screenshots of each beat and an exported
      Jaeger trace for beats 1, 2c, 3a, 5.
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
- [ ] Warm-up game: one full game on `rules`, one on `claude`, so the first
      on-stage call is not a cold connection.
- [ ] Back to baseline, then `./scripts/talk-preflight.sh`: no FAIL.

### T-5min

- [ ] `./scripts/talk-preflight.sh` again (no FAIL; the Jaeger check needs a
      decision from the last 15 minutes).
- [ ] `./scripts/agent-backend.sh status` shows `rules`, `default` threshold,
      `capture on`.
- [ ] Browser tabs open, in order: dashboard Agent tab, Jaeger agent.decide
      search (pre-filled), Jaeger held search, Grafana (optional), terminal.
- [ ] Terminal: an ssh session on the Pi, big font, `cd` to the repo,
      `AGENT_FLAG_FILE` exported (stage stack), `setflag` + `AF`/`RF` pasted,
      history cleared of anything with a key.
- [ ] Notifications off on laptop and phone; phone hotspot on standby.

Stage values: `decision.min_interval_ms=default` (3000 ms),
`decision.act_confidence_threshold=default` (0.8, claude/openai),
`decision.jev.act_confidence_threshold=gate` (0.6, Jev's calibrated bar, #1287),
`decision.capture_content=on`, `decision.claude.timeout_ms=default` (see the
current default, retuned in #1231 from live measurements; `patient` if sonnet
is slow in rehearsal).

---

## 6. Reset to baseline (between rehearsals, and at T-1h)

```bash
./scripts/agent-backend.sh rules
./scripts/agent-backend.sh threshold default
./scripts/agent-backend.sh capture on
./scripts/agent-backend.sh effort auto
setflag "$AF" decision.claude.model haiku
setflag "$AF" decision.openai.base_url vendor     # or default if no vendor
setflag "$AF" decision.min_interval_ms default
setflag "$AF" decision.claude.timeout_ms default  # or patient, if rehearsal said so
setflag "$AF" interventions_allowed ambient
setflag "$AF" interventions_enabled on
setflag "$AF" experiments_enabled off
setflag "$AF" rollout_enabled off
./scripts/agent-killswitch.sh on
# Interventions the agent applied during the run stay in interventions.json
# until something resets them: restore the neutral file (the agent is the only
# writer; it re-reads the file on its next write).
git checkout "$(dirname "$AF")/interventions.json"
# after beat 4b also run the rollout reset from section 4 (incl. the agent restart)
./scripts/talk-preflight.sh
```

`talk-preflight.sh` checks exactly this baseline (`enabled=on`,
`interventions_enabled=on`, `interventions_allowed=ambient`,
`rollout_enabled=off`, `experiments_enabled=off`, `decision.backend=rules`,
threshold `default`, capture `on`, `min_interval_ms` `default`, claude model
`haiku`, `llm.eligible_game_kinds=real_only`). Set `TALK_EXPECT_BACKEND=claude`
if you want to start on Claude. It also WARNs when any flag in the
`interventions.json` next to `$AF` is not at its neutral variant (`none`,
`default`, or `off` for `auto_rubberband`).

`$(dirname "$AF")/interventions.json` is `services/flagd/interventions.json` on
the stage stack and `services/flagd/ci/interventions.json` on the rehearsal
stack.

After the talk on the stage stack, restore the shipped, fail-closed defaults:

```bash
git checkout services/flagd/agent.json services/flagd/rollout.json services/flagd/interventions.json
```

---

## 7. Failure modes and recovery

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| Rows show `~~claude~~ -> rules`, `backend_unavailable` | No key in the agent container (keys are read at startup), or model/base URL flag empty | `talk-preflight.sh` key section; `backend.sh status` | Add the key to `.env.local`, recreate the agent (`... up -d agent`). For openai: add the model/base URL variants |
| `fallback_reason=llm_budget_exhausted` after a minute of remote decisions | Global budget spent (decides + retros share it) | `talk-preflight.sh` budget line | With `AGENT_LLM_MAX_REQUESTS_PER_MINUTE` set, the flag is ignored: the only live lever is `setflag "$AF" decision.min_interval_ms slow` (10 s cadence); a bigger env value needs an agent recreate (recommend 30). Without the env: `setflag "$AF" llm.max_requests_per_minute aggressive` (12/min) or the `stage` variant once #1231 lands |
| Vendor budget / credit exhausted (HTTP 429 / 402) | Account limit | Jaeger: `fallback_reason=error`, `error.type` | Switch to the other vendor (`backend.sh claude` / `openai`); the fallback itself is a talking point |
| `fallback_reason=timeout` | Slow venue network or slow model | `decide.latency_ms` on earlier rows | `setflag "$AF" decision.claude.timeout_ms patient` (5000) or the `openai` equivalent; go back to haiku |
| `fallback_reason=llm_not_eligible` | Game kind is not `real` (a shadow game, or eligibility flag changed) | `game.kind` on the span | Play a menu-started game; `setflag "$AF" llm.eligible_game_kinds real_only` |
| No new rows at all | No game running, kill switch off, or on `rules` and nothing to decide | Is a game running? `agent-killswitch.sh status` | Start a game; `agent-killswitch.sh on` |
| Jaeger tabs / Grafana empty, Decisions panel fine | Pi clock skewed (lookback searches use the clock) | `timedatectl`; preflight clock line | Fix the clock (section 5) |
| Flag flipped, nothing changed | Helpers edit a different dir than flagd serves | `talk-preflight.sh` "flag dir" section | Export the right `AGENT_FLAG_FILE` (stage: `services/flagd/agent.json`); flip again |
| Flag file rejected by flagd (all flags at defaults) | Hand edit broke the JSON | `python3 -m json.tool "$AF"`; `docker compose logs flagd` | Fix the JSON; use the helpers / `setflag`, which validate variants |
| Dashboard Decisions panel empty / stale, Jaeger slow | Jaeger query slow on the Pi (whole game traces) | Open the Jaeger search directly | Narrow the Jaeger lookback; restart jaeger as a last resort (`docker compose restart jaeger`); present from the captured screenshots |
| `held` never appears in 3a | Confidence always above 0.95 | `decide.confidence` on recent rows | Narrate the evaluation event instead; there is no stricter variant |
| `invalid_answer` rows on the vendor | Model does not follow the JSON schema / rejects `reasoning_effort` | `error.type`, `decide.invalid_reason` | `backend.sh effort omit`; pick another model; or skip 2c |
| Whole demo wedged | anything | `docker compose ps` | `backend.sh rules`: rules need no network and no key. Last resort: screenshots and captured traces |

---

## 8. What the audience should take away

| Beat | OpenTelemetry gives | OpenFeature gives |
|---|---|---|
| 1 Observe | The decision, its reason and its confidence as a standard GenAI span (`gen_ai.*` + `agent.decide`), in any OTel backend | (the flags in effect are part of the decision record) |
| 2 Model switch | `gen_ai.request.model` / `gen_ai.provider.name` prove which model decided, per decision | One flag (`decision.backend`, `decision.<vendor>.model`) picks the vendor and model, live, no redeploy |
| 3 Permissions | `held`, `noop`, `discarded` are visible with the reason, and `decide.questions` shows what the model was offered | The confidence threshold, the allow-list (which shapes the question itself) and the kill switch are flags, evaluated at use time |
| 4 Rollback | The trace timeline shows before/after; infra: `remediation.action` tells the story | Rollback = flip the flag back. Infra: the agent itself rolls back by writing a flag |
| 5 Degrade | `decide.fallback_reason` + `error.type` make the degradation explicit | Per-backend timeouts are flags; rules is the floor, the game never waits |
| 6 Jev | A new vendor shows up as a new `gen_ai.provider.name` without dashboard changes | A new backend is a new flag variant |

One line: **OTel is how we see what the agent did and why; OpenFeature is how we
change what it is allowed to do, while it runs. Both are open standards; no
vendor control plane.**

---

## 9. Rehearsal record (fill in on the demo hardware)

| Beat | Measured: flip -> first new row | Measured: `decide.latency_ms` p50 | Notes |
|---|---|---|---|
| 1 rules decisions per minute (4 players) | | n/a | |
| 2a claude haiku | | | |
| 2b claude sonnet | | | |
| 2c vendor | | | |
| 3a held | | | |
| 3c discard caught? (y/n, tries) | | | |
| 4b expand -> rollback | | n/a | |
| 5 fallback (network off / blackhole) | | | |
