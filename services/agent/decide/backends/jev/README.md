# jev decision backend

`decision.backend=jev`: TypeSafe's hosted Jev model through its System One API
(<https://docs.typesafe.ai/api>), called directly by the agent (epic #1225,
issue #1229). There is no Go SDK (TypeSafe ships Python and JS SDKs), so the
client is a plain HTTP POST through `internal/chathttp`.

`Available()` needs only `JEV_API_KEY`. The base URL and model default to
`https://api.typesafe.ai/v1` and `jev-latest`, so `scripts/agent-backend.sh jev`
works with just the key set.

## Two-stage round (#1262)

Jev answers the questions of one call independently, so target and magnitude
cannot depend on the chosen intervention within a call. An FFA request is
therefore split inside `Decide` (`stages.go`); the decision hook still sees one
backend call:

1. **`intervention`**: the **gate** noul and `ffa.intervention` **without
   `noop`** (see [The gate](#the-gate-1281)), plus any other non-detail question
   (e.g. a caller's noul).
2. **`details`**, only when it has something to ask: `ffa.target` and/or
   `ffa.magnitude` conditioned on the chosen intervention. The state carries
   `chosen_intervention`, the instructions name it ("The agent will apply
   grant_shield (...). Which player should receive it?"), the target options are
   the alive players only, each described by their suitability for **that**
   intervention, and the magnitude levels are that intervention's own values.

| stage-1 outcome | stage 2 asks | answered locally (rules adapter conventions) |
|---|---|---|
| gate closed (p_yes <= 0.5): `noop` | nothing (1 call) | target `none` (p=1, confidence 1); magnitude `Range[0]` at the intervention's confidence |
| player intervention with bounds (`grant_shield`, ...) | target + magnitude | nothing |
| player intervention without bounds (`send_controller_effect`, ...) | target | magnitude `Range[0]` |
| session intervention with bounds (`adjust_music_tempo`, ...) | magnitude | target `none` |
| session intervention without bounds (`end_game`, `play_audio_cue`, ...) | nothing (1 call) | target `none`, magnitude `Range[0]` |

Both calls share one `decision.jev.timeout_ms` deadline. Each call is its own
`chat` span with `jev.stage` = `intervention` | `details` (`single` for a
request without `ffa.intervention`, e.g. nouls only).

**A failed details call fails the round** (the caller falls back to rules with
`error` / `timeout` / `invalid_answer`); there is no half answer. The
alternative, the intervention with target `none` at a lower confidence, is
either rejected anyway (a player intervention without a target is
`player_intervention_without_target` in `decision.mapAnswers`) or would apply a
magnitude the model never chose. Rules is the tested floor.

Every choice distribution covers every option of the original question; the
details-stage target answer gets an explicit `none: 0`.

## The gate (#1281)

Asked "which intervention, `noop` included", Jev spreads the acting mass over
several interventions, so `noop` won the argmax even when most of the mass said
"act" (near the death threshold: p(noop) 0.67, the rest split between
`adjust_player_sensitivity` 0.16, `grant_shield` 0.08, ...). Stage 1 therefore
asks two independent questions in the same call:

- the gate, a noul (internal id `jev.gate`, never returned): "Does the game
  need an intervention right now?", with `true` / `false` criteria describing a
  collapsing, dragging or lopsided round vs a round going well on its own
  (`criteria.go:noulCriteria`);
- `ffa.intervention` **without `noop`**: "If the agent intervenes now, which
  intervention fits the game best?".

The `ffa.intervention` answer `Decide` returns is **synthesised**
(`stages.go:gateAnswer`):

```
{noop: 1 - p_yes, <Jev's pick among the interventions>: p_yes, every other option: 0}
```

- the acting confidence `1 - p(noop)` equals **p_yes**, so the existing
  `decision.act_confidence_threshold` gates the gate (no change in `decision/`);
- `decide.Validate`'s argmax rule makes the choice the intervention **iff
  p_yes > 0.5**, else `noop` (confidence `1 - p_yes`); a closed gate makes no
  details call;
- every option of the caller's question is covered.

It is deliberately **not** the factorised `p_yes * q_i`, which would split the
acting mass over the interventions again. The synthesised distribution is what
`decide.answers` on `agent.decide` shows; **Jev's real answers** are on the
stage-1 chat span: `jev.gate.p_yes`, `jev.stage1.choice` and
`jev.stage1.distribution` (JSON, noop excluded). The rationale names both, e.g.
`jev: adjust_player_sensitivity (gate p_yes=0.70; adjust_player_sensitivity p=0.38 among interventions) on MOCK0003 (p=0.89)`.

The gate applies when `ffa.intervention` offers `noop` and at least one
intervention; otherwise stage 1 asks the question as is (and the acting
confidence is `1 - p(noop)` of Jev's answer, as before).

The static game description no longer says "Doing nothing is often right;
intervene only when the game state clearly calls for it": with `noop` out of
the choice it only biased the gate toward no. Restraint now comes from the
gate's calibrated p_yes and the threshold.

### Calibration (2026-10-16, jev-1.13.0, `TestLiveGate`)

p_yes per state (fixture_request_test.go), per wording iteration:

| state | old game text + first criteria | neutral game text + first criteria | **shipped** (neutral text + final criteria), 2 runs |
|---|---|---|---|
| calm (4 alive, all safe, 18 s) | 0.09 | 0.12 | **0.16, 0.16** |
| dragging (2 safe players, nobody out for 130 s) | 0.19 | 0.28 | **0.36, 0.35** |
| collapsing (4 out in 20 s, both survivors near threshold) | 0.34 | 0.42 | **0.67, 0.68** |
| near-threshold (3 alive, MOCK0003 margin 0.10, MOCK0002 dominating) | 0.48 | 0.55 | **0.72, 0.71** (0.70 in the recorded fixture) |

Stage-1 pick among the interventions: calm `set_pacing_profile`, dragging
`play_audio_cue`, collapsing and near-threshold `adjust_player_sensitivity`
(0.24 to 0.45), `grant_shield` second.

**Threshold.** No state reaches the default `decision.act_confidence_threshold`
0.8, so with the default every gated Jev round is `noop` or `held`. The new
variant **`gate` = 0.6** separates the states: near-threshold and collapsing
act, calm and dragging do not (`scripts/agent-backend.sh threshold gate`). It is
not the default: the flag is global, so it also applies to claude and openai.
Dragging stays low because Jev does not compute "nobody eliminated for 130 s"
from the timeline; a precomputed pace signal in `decide.State` is the follow-up.

**Two-way levers** (`adjust_player_sensitivity` 0.7 protect .. 1.5 handicap,
`set_player_handicap` 0.75 rein in .. 1.75 help). Asked independently, Jev could
pick the struggling player AND the handicap end. So the DIRECTION is decided in
Go from the chosen target's signals: `struggling` or `near_threshold` gets the
help / protect side, `most_headroom` the rein-in side, anything ambiguous the
help side (fail-safe). Jev answers only the STRENGTH on each side, as two
3-level half-ladders asked in the same details call (`ffa.magnitude.help` /
`ffa.magnitude.rein_in`, internal ids), and `Decide` keeps the one matching the
chosen target. No third call. Half-ladders run from the neutral (excluded)
outward: sensitivity help 0.9 / 0.8 / 0.7, rein in 1.17 / 1.33 / 1.5; handicap
help 1.25 / 1.5 / 1.75, rein in 0.92 / 0.83 / 0.75.

**No neutral in acting ladders.** A lever's neutral ("no change", e.g. handicap
1.0) is never a magnitude level, so an acting round cannot spend an
intervention on a no-op.

A player intervention with numeric bounds but no alive candidate makes no
details call: target `none` at confidence 0 is returned directly (the decision
hook rejects it as `player_intervention_without_target`).

**Rate.** An acting round makes 2 HTTP calls per `Decide` (a noop round 1), so
the request rate to TypeSafe can exceed the configured per-minute decide /
intervention budget by up to 2x. Size the TypeSafe rate limit accordingly.

## Wire format

```
POST {decision.jev.base_url}/systemone
Authorization: Bearer $JEV_API_KEY
{"model": "jev-latest", "state": {...}, "questions": {"<id>": {"type", "instructions", "criteria"}}}
-> {"model": "jev-1.13.0", "answers": {"<id>": {...}}, "usage": {"input_tokens", "output_tokens"}}
```

The request id comes back only as the `x-typesafe-request-id` header; it becomes
`gen_ai.response.id`.

| our question (`decide`) | Jev primitive | criteria we send | answer mapping |
|---|---|---|---|
| `choice` | `choice` | ordered `{option: description}` (`criteria.go`): lever effects for `ffa.intervention`, one sentence per player for `ffa.target` (status, signals and their rank among the alive players, computed in code), `null` otherwise | `choice`, `probabilities`: these must cover every option exactly (the details-stage target: every alive player), otherwise the answer is invalid. They are rescaled when the sum is within max(0.05, n·0.005) of 1 |
| `score` | `score` | a ladder, lowest to highest setting. Details-stage `ffa.magnitude`: the chosen intervention's own levels (5 evenly spaced values of `decide.MagnitudeBounds`, whole numbers for an integer lever, fewer if its range is narrower), each with what it does. Otherwise 5 position levels (single-stage `ffa.magnitude` lists every allowed lever per level) | Jev's **most likely level**: the argmax of its per-level `probabilities` (#1281); a tie goes to the **gentler** level (nearer the lever's neutral, else the lower level: a shorter shield, a slighter change). Only an answer without level probabilities falls back to rounding the expected `score`. The answer is that level's normalized position, so `decide.ResolveMagnitude` returns exactly the level's value |
| `noul` (new kind, #1229) | `noul` | `{true, false}` criteria for the gate, none for a caller's noul | `Answer.Noul` = P(yes) |

`state` is `decide.State` as a JSON object: a static game description, game
kind, elapsed seconds, the alive serials, the players (with the danger signals
below), the narrative, `most_headroom` / `struggling`, and in the details stage
`chosen_intervention`. Never
`Request.Context`, and not the digest or game id. Non-finite readings are
dropped (Jev rejects them with a 400).

### Danger signals (`decide.State`, all backends)

Jev is weak at comparing numbers, so `decide.BuildState` does the comparison in
Go (`decide/danger.go`). Per alive player: `intensity_rank` (1 = highest current
intensity), `threshold_margin` (death threshold minus intensity, g) and
`danger`, from intensity / `Session.DeathThreshold`:

| danger | ratio | |
|---|---|---|
| `safe` | < 0.60 | |
| `elevated` | 0.60 to < 0.85 | |
| `near_threshold` | >= 0.85 (over the threshold included) | 0.85 is the median of the game's own warning band (SLOW/FAST_WARNING are 0.68 to 0.92 of the death threshold, `games/base.py`) |

Unknown threshold or intensity leaves `danger` empty. Session-level:
`most_headroom` (strictly lowest current intensity: the calmest right now,
**not** "winning", and never a reason to eliminate anyone) and `struggling` (strictly highest intensity, if
elevated or near_threshold). They are in the narrative (so Claude and OpenAI
read them, and the system prompt explains them) and in the digest.

`shielded` marks a player a `grant_shield` / `partial_shield` was dispatched to
within the last 10 s (the longest shield, read from the timeline); the
details-stage target description then says another shield would be wasted.

**Limits.** `danger` reads the SESSION's effective death threshold
(`game_effective_death_threshold`). Per-player sensitivity, handicap factors and
shields are invisible to it, so a player with a protective factor or an active
shield can still show `near_threshold`.

### Option order

Choice options keep `Question.Options` order on the wire. Since the gate
(#1281) the stage-1 `ffa.intervention` has no `noop`: its first option is the
first allowed intervention (`play_audio_cue` in the `full` allow-list), so the
first-option bias now favours that one (re-measure with `TestLiveOptionOrder`,
which moves it last). The table below is the pre-gate measurement, when
`noop` was first.

**Known bias, chosen to fail safe.** The jev-1.13 jaggedness doc says the model
leans toward the first option of a choice. We put `noop` first on purpose, so
any order bias pushes toward doing nothing, never toward acting. Measured on our intervention question (2026-10-16, stage 1,
same state, noop first vs noop last):

| state | p(noop), noop first | p(noop), noop last |
|---|---|---|
| near-threshold (3 alive, one at margin 0.10) | 0.64, 0.60, 0.55, 0.60, 0.64, 0.63 (mean 0.61) | 0.57, 0.59 (mean 0.58) |
| calm (4 alive, all safe, 18 s in) | 0.97, 0.98, 0.99, 0.98, 0.98, 0.98 | 0.97, 0.95 |

The choice was noop in every run either way, so the bias is about 0.03 and does
not suppress interventions; noop stays first (fail-safe). Jev's noop
preference comes from the noop / game descriptions, not the order (see the PR
for a neutral-noop probe).

### Confidence

The `decide` contract says confidence is how sure the backend is that acting on
the answer is right. Jev's `confidence` measures how peaked the distribution
is. For choices it is `(p_max - 1/n) / (1 - 1/n)`.

- `ffa.intervention`: the same acting confidence as the rules adapter, `1 -
  p(noop)` for an intervention and `p(noop)` for noop, computed on the
  synthesised distribution: so **p_yes** of the gate when acting, `1 - p_yes`
  for noop (#1281). Without the gate (no `noop` option to gate) it is computed
  from Jev's own probabilities. Jev's peakedness would penalize
  two strong interventions that together outweigh noop, which the contract
  calls a confident case for acting. With about 15 options it also roughly
  collapses to `p_max`.
- `ffa.target`, `ffa.magnitude`, other choice and score questions: Jev's native
  confidence.
- noul: `|2p - 1|`. Jev returns no confidence for a noul, and this is the formula
  its docs give (the choice formula for two outcomes).

Jev's native value for every choice and score is on the chat span as
`jev.native_confidence`, for example
`{"ffa.intervention":0.75,"ffa.magnitude":0.13,"ffa.target":0.17}`.

### Errors

| outcome | error.type | decision fallback |
|---|---|---|
| 400 / 401 / 413 / 415 / 422 / 429 / 529 / 5xx | the status code (`max_tokens_exceeded` for a 400 over the context length) | `error` |
| transport timeout / cancel | `timeout` / `canceled` | `timeout` / `error` |
| unparseable body, no answers, a missing or wrong-typed answer, out-of-range numbers | `invalid_response` / `no_answers` / `invalid_output` (wraps `decide.ErrInvalid`) | `invalid_answer` |

Error bodies follow the FastAPI shape: `{"detail": {"error_type", "message"}}`
for 401, and `{"detail": [{"type", "loc", "msg", "input"}]}` for 422 (see
`testdata/response_401.json` and `response_422.json`). Jev has no refusal or
finish reason. We do not retry 429/529: the in-game budget is one call.

## Layout

| file | what |
|---|---|
| `wire.go` | endpoint, auth, request/response structs, the mapping to and from `decide.Request` / `decide.Response`, confidence, level snapping |
| `stages.go` | the two-stage split, the details plan, local answers, merging |
| `criteria.go` | the natural-language option and level descriptions and the state's game description |
| `jev.go` | Config and defaults, `Available`, timeout, the HTTP call, GenAI span and metrics |
| `testdata/*.json` | **recorded** Jev traffic (jev-1.13.0, 2026-10-16, #1281): `gate_*_intervention` (gated stage 1 of `testRequest()`: p_yes 0.70, `adjust_player_sensitivity`) and `gate_*_details` (the stage 2 that followed), `shield_*_details` (stage 2 after `grant_shield`), `balance_*_details` (stage 2 after `adjust_player_sensitivity`: target + both strength half-ladders), `*_noul`, 401/422 |
| `contract_test.go` | replays the fixtures (and mutations of them) through `wire.go` and the two-stage `Decide`: gate closed (1 call, noop), gate open (2 calls, conditioned), the p_yes > 0.5 boundary, routing-distribution invariants, most-likely-level magnitude incl. ties, session interventions, stage-2 failure, shared deadline |
| `live_test.go` | `-tags live`: real calls (`TestLiveDecide`, `TestLiveGate`, `TestLiveDetails`, `TestLiveOptionOrder`), latency/tokens log, `JEV_RECORD_FIXTURES=1` re-records `testdata/` |

```sh
JEV_API_KEY=... go test -tags live ./decide/backends/jev -run TestLive -v
JEV_API_KEY=... go test -tags live ./decide/backends/jev -run 'TestLiveGate$' -v  # p_yes per state (4 calls)
JEV_API_KEY=... JEV_RECORD_FIXTURES=1 JEV_LIVE_SCENARIO=near-threshold JEV_LIVE_NOUL=1 go test -tags live ./decide/backends/jev -run 'TestLiveDecide$|TestLiveDetails$' -v  # re-record
```

`TestContract_Request` fails when the encoding changes (criteria wording, state
shape) until you re-record. The recorder refuses to write a body that contains
the key, and the key only ever travels in the `Authorization` header.

## Measured (2026-10-16, from WSL in Vienna, jev-latest = jev-1.13.0)

| round | calls | cold (DNS + TLS) | warm | input / output tokens per call |
|---|---|---|---|---|
| FFA noop round (stage 1 only) | 1 | 280 to 322 ms | 228 to 290 ms | about 1500 / 146 |
| FFA act round (stage 1 + details) | 2 | 525 ms (286 + 239) | 469 ms (250 + 219) | about 1500 / 146, then 1433 / 75 |
| 2 nouls (#1229 PR) | 1 | | 237 ms | 882 / 40 |
| gated stage 1 only (#1281, `TestLiveGate`, 16 samples) | 1 | | 219 to 336 ms (one 910 ms outlier) | about 1500 to 1650 / 161 to 163 |
| gated act round, near-threshold (#1281) | 2 | 567 ms (327 + 238) | 473 ms (230 + 242) | 1622 / 163, then 1601 / 95 |

The act round stays far inside the 2.5 s `decision.jev.timeout_ms` budget and
far below Claude (Haiku about 3.4 s, Sonnet about 3.0 s warm). Jev bills input
tokens only.

## Still unknown

- **Latency SLA** (p50/p95/p99, regions): not documented. The numbers above
  are three samples.
- **W3C `traceparent`**: we send it (`chathttp`), but the docs do not say
  whether Jev joins our trace. Treat it as ignored.
- **Retention**: no zero data retention unless on an enterprise plan (see
  docs/legal). The state carries controller serials only, no personal data.
- **Rate limits**: per organization, in the TypeSafe console. 429 responses can
  carry `retry-after`.

## Caveats from the docs (jev-1.13 jaggedness)

- Questions are evaluated **independently**: hence the two-stage round.
  Conditioning lifted the target from a 0.38/0.38 tie (MOCK0003 vs none) to
  MOCK0003 at 0.99 (confidence 0.98) for `grant_shield`.
- Jev is weak at **numeric comparison and interpolation**: hence the danger
  signals and ranks computed in code, and the most-likely level instead of the
  expected score. Magnitude confidence stays low on the 5-level shield ladder
  (0.15 to 0.31 measured; the #1265 answer 0.29 / 0.28 / 0.30 / 0.10 / 0.03
  had score 1.3, which rounded to level 1 although level 2 is the mode) and is
  higher on the 3-level two-way half-ladders (sensitivity help 0.41 to 0.52).
- **Option order bias** toward the first option: measured small, see above.

## Stage fallback

- With `decision.backend=jev` and no `JEV_API_KEY`, the decision wiring falls
  back to `rules` with `fallback_reason=backend_unavailable`. If Jev is configured but
  unreachable, slow, or returns an invalid answer, the result also comes from
  `rules`, with fallback reason `timeout`, `error` or `invalid_answer`. The game
  never stalls, and the `agent.decide` span shows why.
- To switch away live, flip the backend with `scripts/agent-backend.sh`.
  `scripts/agent-backend.sh jev pinned` pins `jev-1.13.0`.

## Telemetry

One CLIENT span per HTTP call (two for an acting FFA round), `chat
{decision.jev.model}`, tagged `jev.stage` (the gated stage-1 span also carries
`jev.gate.p_yes`, `jev.stage1.choice`, `jev.stage1.distribution`), using the OTel GenAI
semantic conventions (v1.41.0) with the custom `gen_ai.provider.name=jev`.
The span also carries `gen_ai.response.model` (the versioned id), the usage
tokens and `jev.native_confidence`. With `decision.capture_content` it also
records the request body as `gen_ai.input.messages` and the answers JSON as
`gen_ai.output.messages`. Jev has no system prompt.
