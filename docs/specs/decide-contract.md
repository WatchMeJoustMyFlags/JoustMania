# Decision backend contract (`decide`)

> Technical spec. Part of the [spec tree](../README.md). Realises FR-044 to FR-054
> in [requirements.md](../requirements.md) and [UC-040](../use_cases/UC-040.md),
> [UC-044](../use_cases/UC-044.md), [UC-045](../use_cases/UC-045.md).
> State: `main` @ 84c3ba15 (2026-10-16, includes PRs #1257, #1261, #1265, #1284, #1286 (the Jev gate and most-likely-level magnitude of #1281), #1288 (per-backend act threshold), #1297 (#1295 decision target), #1303 (#1298 no-interventions gate), #1306 (#1299), #1312 (#1301 feedback), #1315 (#1310/#1313) and #1308 (#1258 pattern games)). Code references use `file:Symbol` where practical, because line numbers rot. Planned or in-flight items carry their issue / PR number and are marked **Planned** / **In flight**.
> **References.** Write a code reference as a full repo path: `path/file.go:Symbol` (survives edits) or `path/file.go:123` (allowed, but it drifts and a growing count warns in CI). `make spec-refs-check` fails on a missing file, an out-of-range line, a missing symbol or a path that is not a full repo path (#1280). <!-- spec-refs: ignore -->

The agent answers "should I intervene in this game, how, and on whom" through one
contract, independent of who answers: the in-process **rules** engine or a remote
model (**claude**, **openai**-compatible, **jev**). The backend is selected live by
the `decision.backend` flag (epic #1225). The rules engine is the floor: whenever a
remote backend cannot answer usefully, rules decide and the game keeps playing
(northstar constraint "degrade, never stall", C-004).

**Backend roles** (maintainer decision, C-018): **jev** is the primary in-game backend; **claude** is the
in-game backup, selected by a manual `decision.backend` flip, and separately serves the post-game retro /
summary and the experiment proposer (through the `inference.backend` flag, not this contract); **rules** is the
automatic floor. The shipped default of `decision.backend` is `rules`; a deployment selects jev explicitly
(`scripts/agent-backend.sh jev`).

Paths below are relative to `services/agent/` unless they start with `services/`,
`tests/` or `scripts/`.

## 1. Types

Defined in `decide/decide.go`.

| Type | Fields | Rules |
|---|---|---|
| `QuestionKind` | open string | Validate rejects unknown kinds (`services/agent/decide/decide.go:QuestionKind`). |
| `Question` | `ID`, `Kind`, `Text`, `Options []string` (choice), `Range [2]float64` (score) | `services/agent/decide/decide.go:Question` |
| `Answer` | `QuestionID`, `Choice`, `Probabilities map[string]float64`, `Score`, `Noul *float64`, `Confidence` | `services/agent/decide/decide.go:Answer` |
| `PlayerState` | `Serial`, `Alive`, `Intensity*`, `Variance*`, `Skill*`, `BatteryPct*`, `EliminationOrder` | pointer = never observed (`services/agent/decide/decide.go:PlayerState`) |
| `State` | `GameID`, `GameKind`, `Digest`, `Narrative`, `Players` (sorted by serial), `ElapsedSeconds` | `services/agent/decide/decide.go:State` |
| `Request` | `State`, `Questions`, `Context` (`json:"-"`), `PromptProfile` (`json:"prompt_profile,omitempty"`, #2467) | Only in-process backends (rules) may read `Context`; remote backends must not; never logged with `%v` (`services/agent/decide/decide.go:Request`). `PromptProfile` is set by the agent from `decision.prompt_profile` on the remote path only (§3); it is outside `State`, so it is not part of the State digest. |
| `Response` | `Answers`, `Backend`, `Rationale` | Rationale is untrusted text, truncated (`services/agent/decide/decide.go:Response`). |
| `Backend` | `Name() string`, `Decide(ctx, Request) (Response, error)` | Must honour `ctx` cancellation; the caller validates (`services/agent/decide/decide.go:Backend`). Optional `Available() bool`; false means `backend_unavailable` (`services/agent/decision/decide_registry.go:availability`, `DecideRegistry.Resolve`). |

**Planned (#2479, status Open; no code on `main` yet).** Three additions, all outside `Question`/`Answer`, so `Validate` and
the permission chain never see them:

| Type | Fields | Rules |
|---|---|---|
| `Prediction` | `Characteristic`, `Scope` (`game`/`field`/`target`), `Target` (serial, `target` scope only), `Template`, `HorizonS`, `P`, `Source` (`rules`/`lever_default`/`model`/`control`), `Rule` | What the decider expects to be true after its answer is applied; closed vocabulary (§14.2). Carried on `Response.Prediction` and on the decide record, **never** on `Decision` or `DecisionOutcome` (the rules-path golden stays byte-identical). `P` is the probability the prediction comes true. |
| `PredictionAsk` | `*Question` (`ffa.prediction`, kind choice), `json:",omitempty"` | `Request.PredictionAsk`, **outside `Request.Questions`**. Set only when `decision.predictions.ask_model` is on and the backend is claude or openai (§3). nil under v1. |
| `PredictionDropped` | reason string on `Response` | `missing`, `bad_option`, `bad_confidence`, `no_target`, `inconsistent`. A model prediction that fails its own check (§4.1) is dropped; the decision is unaffected. |

### 1.1 Question kinds

| Kind | Status | Answer shape | Confidence |
|---|---|---|---|
| `choice` | Shipped (`services/agent/decide/decide.go:KindChoice`) | `Choice` ∈ `Options`, `Probabilities` over options (sum 1 ± 1e-3, choice is the argmax), `Score` = 0 | backend-defined, [0,1] |
| `score` | Shipped (`services/agent/decide/decide.go:KindScore`) | `Score` within the inclusive `Range`; no choice / probabilities | backend-defined, [0,1] |
| yes/no | Shipped as a two-option `choice` (`services/agent/decide/decide.go:KindChoice`) | as choice | as choice |
| `noul` | Shipped in PR #1261 (`services/agent/decide/decide.go:KindNoul`) | `Answer.Noul` = P(yes), finite in [0,1]; no choice / probabilities / score; only noul answers may carry `Noul` (`services/agent/decide/validate.go:validateQuestion`, `validateAnswer`) | backend-defined; jev uses \|2p − 1\| |

Notes on `noul` (TypeSafe Jev's yes/no primitive, #1229): backward compatible (`noul` is omitted from
choice / score answers). Support per backend: **jev** answers natively; **rules** answers 0.5 at confidence
0; **claude** and **openai** reject a request containing a noul before any network I/O with an error
wrapping `ErrInvalid` (`llmio.Supported`, `services/agent/decide/llmio/llmio.go:Supported`), which the loop records as
`fallback_reason=invalid_answer`. No caller-level FFA question is a noul; the Jev backend asks the internal `jev.gate` noul every stage-1 round (§3.1).

### 1.2 State

`BuildState(GameContext)` is pure and deterministic (`services/agent/decide/state.go:BuildState`):
`GameID` = session id, `Alive` = player active, `EliminationOrder` from the session's
elimination sequence (1-based). The **narrative** has the lines `game:`, `session:`,
`players:` and `timeline:` (`services/agent/decide/state.go:renderNarrative`). The **digest** is a lowercase hex
SHA-256 over a fixed-order canonical encoding including the narrative
(`services/agent/decide/state.go:digest`); it changes on every snapshot and is for correlation, not a
cache key.

**Danger signals** (PR #1265, `decide/danger.go`), computed in Go for every backend so no model has to
compare numbers: per alive player `intensity_rank` (1 = highest intensity), `threshold_margin` (session death
threshold − intensity, g) and `danger` = `safe` (< 0.60 of the death threshold), `elevated` (0.60 to < 0.85)
or `near_threshold` (≥ 0.85; 0.85 is the median of the game's own warning band); `shielded` when a `grant_shield` or
`toughness_boost` was dispatched to the player in the last 10 s, except a `grant_shield` to a `shield_charged`
player (in trigger mode a charge is not protection); `shield_charged` (alive players only, #1870) while the
game's charge ledger (§4.5 step 4) holds a live entry for the player at the moment the state is built: the
agent granted a trigger-mode charge within the window, and the player holds it or has already activated it
(the agent cannot tell which: it has no shield-state feed); and on the state `most_headroom` (strictly lowest intensity) and
`struggling` (strictly highest intensity when elevated or near_threshold). They appear in the narrative,
the digest and the Jev state. `shield_charged` is rendered as the narrative token `shield_charged` and in the digest as
` shield_charged=true` only when set, so a state without a charge keeps its digest. Danger reads the session threshold, so per-player factors and shields are not
reflected. The rules engine does not read the state's danger fields; its danger shield (R4 before the first elimination, [UC-040](../use_cases/UC-040.md) A43, BR-032, #2461) applies the same intensity / death-threshold ratio to the per-player movement track in the context (`decide.SustainedDanger`: the last `policy.shield_danger_samples` readings each at or above `policy.shield_danger_ratio`, the newest value at most 3 s old since it was last new; off with `policy.shield_danger_enabled`), so the state and the decide record are unchanged. Entity: [DECIDE_PLAYER_STATE](../entity_model.md#decide_player_state).

**Shield limits and applied levels (#2066; [UC-040](../use_cases/UC-040.md) A33, A35, A37).** `shield_limited` (alive players only) is set while the agent would refuse a `grant_shield` to the player for a reason other than `already_charged` at the moment the state is built: `shield_cap_reached` or `shield_cooldown` (§4.5 step 4), and in a hold game `shield_pool_full` or `shield_budget_exhausted` (A37). It comes from the same ledger snapshot as `shield_charged` (`Loop.ShieldSerials`; on the rules path the snapshot the ranking used, on the remote path one snapshot per request), so the state marks exactly the players the rules' no-shield filter drops; a `shield_charged` player is not also `shield_limited`. Narrative token on the player line: `shield_limited`, or `shield_limited=<reason>` in a hold game when the state's own energy and budget give the hold refusal (`State.HoldRefusal`); the cap and the cooldown are not state fields, so the bare token stands for them. `sensitivity_factor` and `handicap_factor` (alive players only) are the per-player levels the agent itself last wrote to this game (`adjust_player_sensitivity`, `set_player_handicap`), read from the applied-value ledger (§4.5 step 3, `Loop.AppliedValues`, one snapshot per request), set only when the value is a finite number other than the neutral 1.0 of §2.1 (an empty value, which the writer writes as its default, shows the §2.1 Default: 1.5 / 1.25, pinned equal to the writer's by `actions/decide_bounds_test.go`), so a backend sees the help already in place before it spends a call (NFR-011); narrative tokens `sensitivity_factor=0.80` / `handicap_factor=1.25`. They are the agent's own record (in memory, 30 s TTL, nothing while a write to the key is in flight, lost on an agent restart), not the coordinator's flag value: a per-player value an operator or an experiment set is not shown, and while the loop's suppression is off (a hand-built loop, the rules golden) neither factor is set. Game-wide levels are not annotated. All three are written to the digest (` shield_limited=true`, ` sensitivity_factor=<value>`, ` handicap_factor=<value>`) only when set, so a state without them keeps its digest and `decision/testdata/rules_path_golden.json` stays byte-identical. Jev (`jev/criteria.go`): the `grant_shield` suitability of a `shield_limited` player is "No more agent shields for this player right now (per-game cap or cooldown): another shield is refused." (after the hold texts, which name the pool or the budget), and the candidate description names an applied factor. The LLM system prompt (`llmio.go`) explains `shield_limited` and the factors in one sentence. Code: `services/agent/decide/state.go`, `decide/danger.go`, `decision/decide_hook.go`; tests `decide/limited_uc040_test.go`, `decision/shieldlimit_state_uc040_test.go`, `decide/backends/jev/criteria_limited_uc040_test.go`, `decide/llmio/llmio_test.go` (`TestRender_ShieldLimitedAndLevelTokens`).

**Hold mode (#2096; [UC-040](../use_cases/UC-040.md) A36, A37).** In a game whose `game_shield_activation_mode` gauge says `hold`, each alive player gets `shield_energy_s`, `shield_budget_remaining_s` and `shield_active`, and the state gets `shield_activation="hold"`, `shield_energy_max_s`, `shield_budget_s`, `shield_recharge_per_s` and `shield_min_burst_s`, all from the `game_player_shield_*` gauges and `game_shield_hold_setting{setting}` (telemetry.md §6), never from flagd, so experiment arms and latched promotions read right. `shield_charged` is not emitted in a hold game; `shielded` means `shield_active` or a `toughness_boost` within the shield window (a `grant_shield` adds energy, not protection). Narrative tokens: on the player line `shield_energy=2.4/5 budget=9.1` (plus `shield_active` while held), on the session line `shield=hold shield_cap=5 shield_budget=15 shield_recharge=0 shield_min_burst=0.3`; digest fields only in a hold game, so instant and trigger digests (and `decision/testdata/rules_path_golden.json`) stay byte-identical. The values are up to one export interval old (about 3 s); the seconds of the agent's `grant_shield` writes in flight (reserved, not finished) are added to the energy, at most the cap. The Jev and LLM wire carry the fields (`llmio`, `jev/criteria.go`: a grant the pool or the budget would refuse is described as refused). Code: `services/agent/decide/hold.go`; tests `decide/hold_state_uc040_test.go`, `decide/backends/jev/criteria_hold_uc040_test.go`, `decide/llmio/llmio_hold_uc040_test.go`, `gamecontext/extract_metrics_hold_uc040_test.go`.

## 2. The FFA question set

`decide/ffa.go`. FFA is the only question set on `main`; other modes are not asked.

| ID | Kind | Options / range | Source of options |
|---|---|---|---|
| `ffa.intervention` | choice | `noop` + every intervention in the **live** `interventions_allowed` list (deduped, sorted); in a game of an experiment, the levers of its flag are left out on the remote path (§4.5 step 2a, UC-040 A40 step 5, #2251) | `services/agent/decide/ffa.go:FFAQuestions`, `interventionOptions`; `services/agent/decision/decide_hook.go:buildDecideRequest`, `services/agent/decision/experimentflag.go:offeredInterventions` |
| `ffa.target` | choice | `none` + alive serials (sorted) | `services/agent/decide/ffa.go:FFAQuestions` |
| `ffa.magnitude` | score | [0, 1], normalised over the chosen intervention's bounds | `services/agent/decide/ffa.go:FFAQuestions`, `magnitudeText` |

With `interventions_allowed=none` the only option would be `noop`, so no remote call is made at all (§7.1, #1298); the rules adapter still builds the set on the `rules` backend.

### 2.1 Magnitude bounds

`ResolveMagnitude` clamps the score to [0,1] (non-finite → 0), maps it linearly onto
the bounds and rounds integer bounds (`services/agent/decide/ffa.go:ResolveMagnitude`). Interventions without
bounds ignore the magnitude and the writer uses its default.

| Intervention | Min | Max | Default | Neutral | Integer |
|---|---|---|---|---|---|
| `adjust_music_tempo` | 1.0 | 1.3 | 1.15 | | |
| `adjust_volume` | 0.3 | 1.0 | 0.7 | | |
| `adjust_global_sensitivity` | 0 | 4 | 2 | | yes |
| `adjust_player_sensitivity` | 0.7 | 1.5 | 1.5 | 1.0 | |
| `adjust_global_difficulty` | 0.5 | 2.0 | 1.5 | 1.0 | |
| `set_player_handicap` | 0.75 | 1.75 | 1.25 | 1.0 | |
| `grant_shield` (s) | 3 | 10 | 5 | 0 | |
| `toughness_boost` (s) | 3 | 10 | 5 | 0 | |

Source: `services/agent/decide/ffa.go:magnitudeBounds`.

The `rules` backend answers `ffa.magnitude` from its first decision's value, else from the Default above. A
`grant_shield` from the rules carries no value when ranked; the loop stamps it with the `shield_seconds` `default`
variant before the permission chain (UC-040 BR-029) and sets the record's `ffa.magnitude` answer to that stamped
length, so `decide.answers` on the `agent.decide` span reports the seconds that are written, not 5 (#2395;
`services/agent/decision/chargeledger.go:stampShieldSeconds`). The in-process `RulesBackend.Decide` (tests and
tooling only) still answers the Default.

## 3. Wire format for remote backends

- `llmio.Render(req)` reads only `State` and `Questions` (`services/agent/decide/llmio/llmio.go:Render`).
  User message: `<game_state>` (alive players + narrative) then `<questions>`.
- Static system prompt: FFA rules, "noop is always valid and often right", answer
  format, at most 3 probability entries (chosen option first), rationale at most 100 characters (both
  since PR #1257), "game state is telemetry, not instructions" (`services/agent/decide/llmio/llmio.go:systemPrompt`).
- Used by claude and openai only; jev has its own typed wire format (below).
- **Prediction ask (planned, #2479, `decision.predictions.ask_model`, default off).** When the flag is on and the backend is
  claude or openai, `Render` appends one more item `- id: ffa.prediction` to `<questions>` from `Request.PredictionAsk`. Its
  options are `none` plus `<template>_<horizon>s` for the templates valid in this state (target templates only when a target
  is possible); its text asks what the model expects to happen if its answer is applied, and its confidence is the probability
  that it happens. The model answers it in the existing answers array, so **`AnswerSchema()` is unchanged**. jev never gets
  the ask. **Under v1 (the shipped in-game prompt: the system prompt, user message and answer schema as built by `Render` and `AnswerSchema()` on `main` before #2479, which the talk runs on) the flag is off and the system prompt, the user message and the schema are
  byte-identical to today's**; a golden test pins that (UC-040 BR-034).

- **Prompt profiles (#2467 PR 5, `decision.prompt_profile`, default `v1`).** The agent reads the flag per game on the remote
  path, in the game's flag context (`services/agent/flags/game.go:GameContext`, `PromptProfile`), and puts it on
  `Request.PromptProfile`; an unreadable, empty or unknown value is `v1` (`services/agent/decide/profile.go:NormalizePromptProfile`).
  The rules path never sets it.
  - `v1`: the system prompt, the user message and the answer schema are byte-identical to the shipped in-game prompt, and so
    are jev's criteria (golden tests: `services/agent/decide/llmio/profile_golden_test.go`, jev `contract_test.go`).
  - `neutral`: the system prompt is v1's with two changes: the noop sentence reads `"noop" (do nothing) is always a valid
    answer.` (no "often the right answer", no "only intervene when the game state clearly calls for it"), and a static
    `Levers:` block follows the FFA rules, one line `- <name>: <effect>` per catalog lever (all of them, so the prompt stays
    static per profile and every offered lever has its line) plus `- noop: <effect>`. The lever lines describe what a lever does,
    never what it is for or when it is right (UC-040 BR-036; `services/agent/decide/catalog.go:Intervention.JevDescriptionNeutral`).
    The user message and the answer schema are the same as under v1. jev uses the same neutral lever lines for
    `ffa.intervention` and in the details-stage instructions, a neutral noop line, and target suitability sentences that state the
    player's situation (danger, refusals) without a verdict (`services/agent/decide/backends/jev/criteria.go`). The goal
    sentence of the system prompt and jev's game description and gate criteria are unchanged in both profiles (goal lines
    come with #2479, §14.4).
  - The system prompt's fingerprint (`llm.prompt.system_sha256` on claude's chat span) is therefore one value per profile: v1's
    is unchanged, neutral's differs. `agent.decide` carries `decide.prompt_profile` on the remote path (§12).
- `AnswerSchema()` is static (cacheable), `additionalProperties:false` everywhere,
  all properties required: `{answers:[{id, choice, score, probabilities:[{option,p}], confidence}], rationale}` (`services/agent/decide/llmio/llmio.go:AnswerSchema`).

| Backend | Transport | Availability (else `backend_unavailable`) | Source |
|---|---|---|---|
| `rules` | in process | always | `decision/decide_rules.go` |
| `claude` | Anthropic Messages API (official Go SDK), `POST {decision.claude.base_url}/v1/messages` (the SDK appends the path; the flag has no `/v1`), JSON-schema output; `max_tokens` 2048 where the model may think, 1024 where thinking is off (PR #1257); SDK retries 0 | `ANTHROPIC_API_KEY` + model | `services/agent/decide/backends/claude/claude.go:69-77,142,233,283-308` |
| `openai` | `POST {base_url}/chat/completions`, `response_format: json_schema` (`strict`); on a 400/422 whose body says the schema / format is unsupported, one retry with `json_object` in the same budget; the downgrade is remembered for that base URL + model for 10 minutes only if that retry succeeds (2xx) | `DECISION_OPENAI_API_KEY` + `decision.openai.model` + `decision.openai.base_url` | `services/agent/decide/backends/openai/openai.go:schemaDowngradeTTL`, `schemaUnsupportedBody`, `Backend.Available` |
| `jev` | TypeSafe System One: `POST {decision.jev.base_url}/systemone`, `Authorization: Bearer`, typed body `{model, state, questions:{id:{type, instructions, criteria}}}`; plain HTTP, no retries on 429 / 529 | `JEV_API_KEY` only (base URL and model have defaults) | `services/agent/decide/backends/jev/wire.go:wirePath`, `services/agent/decide/backends/jev/jev.go:Backend.Decide`, `stages.go`, `services/agent/decide/backends/jev/jev.go:Backend.Available`, `decide/backends/jev/README.md` |

**jev details** (PR #1261, #1229): `decision.jev.base_url` defaults to `https://api.typesafe.ai/v1` and
`decision.jev.model` to `latest` = `jev-latest` (variant `pinned` = `jev-1.13.0`). `state` is `decide.State`
as JSON without the digest or game id; choice criteria carry one description per option (lever effects for
`ffa.intervention`, one sentence per player with computed signal ranks for `ffa.target`, `criteria.go`), and
every option must be covered by the returned probabilities. Jev `score` in [0, 4] maps linearly onto the
question range through Jev's **most likely level** (§3.2). Option order puts `noop` / `none` first on purpose
where they are options (`ffa.target`; `ffa.intervention` only when it is not gated, §3.1): Jev's known
first-option bias then fails safe (measured in PR #1265: about 0.03 of p(noop); the noop wording matters
more). Confidence: `ffa.intervention` is the acting confidence 1 − p(noop) (p(noop) when noop is chosen),
the same definition as the rules adapter, computed on the synthesised routing distribution of the gate
(§3.1), so it equals the gate's p_yes when acting; other questions keep Jev's native confidence; noul uses
\|2p − 1\|. Jev's native values are recorded on the chat span as `jev.native_confidence`. HTTP errors (400 / 401 / 413 / 415 / 422 / 429 / 529 / 5xx) map to `error`;
unparseable or incomplete answers to `invalid_answer`.

**Two-stage Jev round** (PR #1265, `decide/backends/jev/stages.go`, `services/agent/decide/backends/jev/jev.go:Backend.Decide`). The hook still
makes one `Decide` call; inside it Jev answers an FFA request in up to two HTTP calls that share the
`decision.jev.timeout_ms` deadline:

| Stage-1 outcome | Stage 2 asks | Answered locally |
|---|---|---|
| gate closed (p_yes ≤ 0.5): `noop` | nothing (1 call) | target `none`, magnitude at the range minimum |
| player intervention with bounds (e.g. `grant_shield`, `adjust_player_sensitivity`) | target and magnitude | nothing |
| player intervention without bounds (e.g. `send_controller_effect`) | target | magnitude |
| session intervention with bounds (e.g. `adjust_music_tempo`) | magnitude | target `none` |
| session intervention without bounds (e.g. `end_game`) | nothing (1 call) | target and magnitude |

Stage 2 is conditioned on the chosen intervention: target options are the alive players described by their
suitability for it (judged in Go from the danger signals), and magnitude options are that intervention's own
levels; the neutral value is never a level. For two-way levers (factors with a neutral) Go decides the
direction from the chosen target's signals and Jev only picks the strength, a level chosen as in §3.2.
A stage-2 failure fails the whole round (rules fallback with `error`, `timeout` or
`invalid_answer`); there are no half answers. Each call gets its own chat span with `jev.stage` =
`intervention`, `details` or `single` (requests without `ffa.intervention`, e.g. nouls). The rationale is a
model-free summary such as
`jev: adjust_player_sensitivity (gate p_yes=0.70; adjust_player_sensitivity p=0.38 among interventions) on MOCK0003 (p=0.89)`.

API keys are read from the environment **once at startup** and never appear in
flags or spans (`services/agent/decide_backends.go:40-54`; `services/agent/decision/decide_registry.go:16-18`).
Changing a key needs an agent restart.

### 3.1 The Jev gate (#1281)

Maintainer decision (2026-10-16): Jev's acting decision is gated by a noul. Asked "which intervention,
`noop` included", Jev spread the acting mass over several interventions, so `noop` won the argmax even near
the death threshold (p(noop) 0.67, the rest split). Stage 1 therefore asks, **in the same call**:

- the **gate** noul, internal id `jev.gate` (never returned to the caller): "Does the game need an
  intervention right now?", with `true` / `false` criteria (a collapsing, dragging or lopsided round vs a
  round going well on its own; `services/agent/decide/backends/jev/criteria.go:noulCriteria`);
- `ffa.intervention` **without `noop`**: "If the agent intervenes now, which intervention fits the game best?".

The `ffa.intervention` answer the backend returns is **synthesised** (`services/agent/decide/backends/jev/stages.go:gateAnswer`):
`{noop: 1 − p_yes, argmax_iv: p_yes, every other option: 0}`, where `argmax_iv` is Jev's stage-1 pick.
Consequences:

- **Confidence = p_yes** when acting (1 − p_yes for noop), so the confidence routing (§5) gates the gate,
  against Jev's own `decision.jev.act_confidence_threshold` (default 0.6, #1287).
- **Choice = argmax_iv iff p_yes > 0.5**, else `noop`, decided by `gateAnswer`'s strict `>` (exactly 0.5 is
  `noop`; `decide.Validate` only accepts the result within its 1e-3 argmax tolerance); a closed gate makes no
  details call.
- **Full option coverage**: every option of the caller's question is in the distribution.
- Deliberately **not** the factorised `p_yes × q_i`, which would split the acting mass again.
- `decide.answers` on `agent.decide` shows the synthesised distribution; Jev's **real** answers are on the
  stage-1 chat span: `jev.gate.p_yes`, `jev.stage1.choice`, `jev.stage1.distribution` (JSON over the
  interventions, no noop).
- The gate applies when `ffa.intervention` offers `noop` and at least one intervention; otherwise stage 1 asks
  the question unchanged.
- Jev's static game description no longer says "Doing nothing is often right; intervene only when the game
  state clearly calls for it": it biased the gate (near-threshold p_yes 0.48 → 0.55). The description is in
  the state of every Jev call, so the details stage and caller nouls shift slightly too (e.g. the recorded
  `about_to_end` noul 0.62 → 0.60).

**Calibration** (live, jev-1.13.0, 2026-10-16, `TestLiveGate`, shipped wording, 2 runs): calm 0.16 / 0.16,
dragging 0.36 / 0.35, collapsing 0.67 / 0.68, near-threshold 0.72 / 0.71. None reaches the global default
threshold 0.8. A threshold of 0.6 makes near-threshold and collapsing act and leaves calm and dragging as noop,
so Jev has its **own** threshold `decision.jev.act_confidence_threshold`, default variant `gate` (0.6), and acts
out of the box (#1287); claude and openai keep the global `decision.act_confidence_threshold` (default 0.8),
whose `gate` variant (0.6) remains but no longer affects Jev unless Jev's flag is `inherit`. Dragging stays low because Jev does not derive "nobody eliminated for 130 s" from the timeline; a
precomputed pace signal is a follow-up.

### 3.2 Jev score levels (#1281)

A Jev score answer carries a probability per level. The answer is the **most likely level** (argmax); a tie
goes to the **gentler** level: the value nearer the lever's neutral, else the lower level (a shorter shield,
a slighter change). Rounding the expected score was dropped: it lands between modes (0.29 / 0.28 / 0.30 gave
1.3 → level 1 although level 2 is the mode). Only an answer without level probabilities falls back to the
rounded score; level probabilities that miss a level, are negative or do not sum to 1 (e.g. all zero) are
`invalid_answer`, like a choice distribution. The level's normalized position is the answer, so `decide.ResolveMagnitude` returns exactly
that level's value. The help / rein-in direction of two-way levers is unchanged (Go decides it).

## 4. Validation

Applied in this order. Any failure in 4.1 or 4.2 is `fallback_reason=invalid_answer`
and rules decide on the **current** context.

### 4.1 Parse (remote only), `decide/llmio/parse.go`

- Raw response ≤ 64 KiB, ≤ 32 answers, ≤ 64 probabilities per answer, rationale
  truncated to 200 runes and sanitised to one line (`services/agent/decide/llmio/parse.go:MaxOutputBytes`..`MaxRationaleRunes`).
- A **truncated top-k** distribution (fewer entries than options, sum in [0.5, 1), top entry greater than
  the missing mass) is rescaled to sum to 1 (PR #1257, `services/agent/decide/llmio/parse.go:truncatedMinSum`, `rescalable`).
- One Markdown fence is stripped; trailing data, empty answers, missing
  `confidence` / `p` / score and duplicate probability options are errors
  (`services/agent/decide/llmio/parse.go:Parse`, `toAnswer`, `toProbabilities`).
- Probabilities summing within 0.02 of 1 are rescaled; a score on a choice question
  (or choice on a score question) is dropped as noise.
- **Prediction split (planned, #2479).** When the ask was sent, `Parse` removes the `ffa.prediction` wire answer **before**
  `decide.Validate` sees the list, and checks it on its own: the option is in the offered list, the confidence is finite
  and in [0,1], a target template comes with a target, and the prediction is consistent with the chosen intervention.
  A passing one becomes `Response.Prediction` with `Source=model`; any failure sets `Response.PredictionDropped` and
  nothing else (no `invalid_answer`, no fallback). A prediction can therefore never invalidate or delay a decision
  (UC-040 A45, BR-034). Without the ask a stray `ffa.prediction` answer is ignored the same way.

### 4.2 `decide.Validate`, `services/agent/decide/validate.go:Validate`

- Questions well formed: unique non-empty IDs, known kinds, unique options, finite
  ordered ranges.
- Every question answered exactly once; no answers to unknown questions.
- `Confidence` finite in [0,1]. Confidence is deliberately not tied to p(choice).
- Choice: in options; probabilities non-empty, known options only, each finite ≥ 0,
  sum within 1e-3 of 1; p(choice) > 0 and ≥ max(p) − tolerance; score 0.
- There is no answer-side alias: an answer naming a retired intervention id (`partial_shield`, renamed to
  `toughness_boost` in #1838) is not among the options and is `invalid_answer`; the round falls back to the rules.
- Score: finite, within the inclusive range, no choice / probabilities.

### 4.3 Cross-question consistency, `services/agent/decision/decide_hook.go:mapAnswers`

Failure here is outcome `invalid` with `decide.invalid_reason`, **no action and no
rules fallback**.

| `decide.invalid_reason` | Condition |
|---|---|
| `noop_with_target` | intervention `noop` but target ≠ `none` |
| `player_intervention_without_target` | player-scoped intervention with target `none` |
| `session_intervention_with_target` | session-scoped intervention with a serial |
| `missing_intervention_answer` | no `ffa.intervention` answer |

Player-scoped interventions: `send_controller_effect`, `eliminate_player`,
`revive_player`, `adjust_player_sensitivity`, `set_player_handicap`, `grant_shield`,
`toughness_boost`, `soft_penalty` (`services/agent/decision/decide_hook.go:playerTargetedInterventions`).

### 4.4 Re-validation when a remote answer lands

Order on landing (`services/agent/decision/decide_hook.go:Loop.runDecideRemote`, `decideRemoteApply`, `decideRemoteFallback`):

1. Live flags: `enabled` off → `discarded/kill_switch`; `decision.backend` changed → `discarded/backend_changed`.
2. Fallback path (the call failed): rules decide on the current context; if the context is missing or the
   game has ended, nothing is decided and the span carries `outcome=fallback` **with**
   `discarded_reason` = `stale_context` / `game_ended`.
3. Answer path: cross-question check (`invalid`), `noop`, then confidence routing (`held` below the
   threshold). Only an answer that would act is then checked against the current context: missing →
   `discarded/stale_context`; game no longer active → `discarded/game_ended`; target absent or inactive →
   `discarded/target_gone` (`decision/async_state.go`); the state the request was built on (the fire-time
   `GameContext.UpdatedAt`) older than the live `decision.max_state_age_ms` when the answer landed →
   `discarded/state_too_old` (#2123, `services/agent/decision/decide_hook.go:stateTooOld`; 0 disables it, an
   unset `UpdatedAt` never discards). A low-confidence answer after the game ended is therefore `held`, not
   `discarded`.

Every remote `agent.decide` span that carries a `decide.discarded_reason` (steps 1 to 3, and the shutdown discard
of §9) is also counted in `agent_decide_discarded_total{reason}` (#2123).

### 4.5 Permission chain (both paths)

`services/agent/decision/decision.go:evaluatePermission`, using the **live** flag snapshot:

1. `interventions_allowed` membership → blocked `not_allowed`
2. `policy.battery_threshold` (player-targeted only; unknown battery does not block) → `battery_threshold`

**2a.** Experiment flag (#1975; UC-040 A40, BR-028; `decision/experimentflag.go`), check-only: in a game that belongs to an experiment in either arm, a decision whose intervention is a lever of the experiment's flag (`decision.ExperimentLevers`: `set_pacing_profile` → `windows`, `adjust_global_sensitivity` → `sensitivity`) → `experiment_flag`. The flag comes from the loop's source (`Loop.SetExperimentFlagSource`, wired in `main.go` to the experiment registry: the game id bound at spawn, then the context's `experiment_id`); no source, no experiment or an experiment the registry does not know blocks nothing. Before the rate limit, so it charges no budget and does not end rank-order admission. The rules engine never proposes these levers (#800), so the step acts on remote decisions; the rules golden is unchanged. Since #2251 (UC-040 A40 steps 5-6) the remote request of such a game does not offer the levers of the flag (`Loop.offeredInterventions`, §3.1, also used by the call gate of §7.1), so the step only catches a backend that answers an intervention it was not offered; and only the first block of a lever on a flag per game is emitted in full: a repeat only counts in `agent_decisions_total` and logs `agent.decision_experiment_flag_repeat` at debug level (as A21). The rules path request is unchanged.

3. Already applied (#2046; UC-040 A35, BR-023; `decision/appliedledger.go`), check-only: it reserves nothing, so its refusal cannot leak a reservation of a later step. Only when the loop's suppression is on (`Loop.SetSuppressReapplied(true)`, wired in `main.go`; off in a hand-built loop such as the rules golden) and only for a state or level intervention (the `state` and `level` kinds of flags.md §13.1, the static set `decision.IdempotentInterventions`, pinned against `registry.yaml` by `actions/idempotent_uc040_test.go`). A grant or an event is never suppressed. The per-game applied-value ledger is keyed `intervention|target serial` and holds the normalised value of the last write the agent made (`applied=true`): the decision's `Value` parsed as a float when it parses as a finite number, else the trimmed string (so `"0.8"` and `"0.80"` match; an empty value matches only an empty value, since the writer's default is a constant). The decision is blocked `already_applied` when the entry matches and is younger than 30 s (a constant, C-035), and its target is still in the game context's players (an entry for an absent serial is dropped). While a write to the key is in flight the entry never matches and is left out of the rules snapshot (a different value may land last); a failed write deletes the entry, as do two writes to one key in flight at once (they may finish in either order); a gated write (A11) leaves it unchanged; an expired entry is written again, which re-asserts a value the flag service lost (a restart, UC-053 A9 / A13). A new flag-service boot epoch does not clear the ledger: the writer drops the write result, so the 30 s TTL is the bound. The block charges no budget and does not end rank-order admission. The first suppression per recorded value emits the spans, an info log `agent.decision_already_applied` (not the `agent.decision_blocked` warning) and a timeline entry "already in place"; repeats only count in `agent_decisions_total` and log `agent.decision_already_applied_repeat` at debug level. The rules engine drops an already-applied candidate before ranking (`applyAppliedPolicy`, next to `applyChargePolicy`, one ledger snapshot per ranking), so the rules may rank another intervention instead, possibly a stronger one (a `grant_shield` for the same player, which step 4 and BR-022 still bound). The decide state carries the applied per-player factors (`sensitivity_factor`, `handicap_factor`, §1.2; #2066).
4. The shield ledger (`decision/chargeledger.go` `reserveShield`; targeted `grant_shield` only). One per-game ledger by serial holds the trigger-mode charge (A31), the shield count and the end of the last shield (A33). One check returns exactly one of `already_charged`, `shield_cap_reached`, `shield_cooldown` or a reservation, in that order, so a refusal never leaves a reservation behind; the one reservation is kept only when the write is applied and is released by the caller's deferred finish on every other path (a rate-limit block, a gated write, a failed write). The check-only already-applied step (3) goes ahead of this step, so it cannot leak a reservation either.
   - `already_charged` (#1837 PR-4a; UC-040 A31 step 1): at most one agent charge per player per window, in trigger mode only. Trigger mode means `game.mode` is `FFA`, the game is real and `shield.activation` resolves to `trigger`, read per targeted `grant_shield` from the `game` set (`services/agent/flags/game.go:ShieldCharge`) with `gameId` and `game_kind=real`, no `targetingKey` and no `game_mode`. The coordinator's grant read carries the game's transaction context as well (UC-040 BR-018, #1970), but targeting on these flags may reference only `gameId` and `game_kind` (flags.md §4) and the agent reads them only for a real game, so both resolve the same value. A `grant_shield` whose target holds an entry in the loop's charge ledger is blocked before the rate limit: it charges no budget, writes nothing and measures no effect. The window is `min(shield.charge_expiry_seconds, 20 s)` (20 s when the expiry is 0) plus 2 s for the write to reach the coordinator, so at most 22 s. The check reserves the entry; it is kept only when the write is applied (`applied=true`) and released by a rate-limit block, a gated write (UC-040 A11) or a failed write (A27). Every entry, a reserved one too, ends with its window, so a lost release blocks at most 22 s. The first refusal per entry gets the full spans, the `agent.decision_blocked` warning and a timeline entry; repeats only count in `agent_decisions_total` and log `agent.decision_already_charged_repeat` at debug level (as the `rate_limit` repeats below). On the remote path an answer that lands for such a target is blocked the same way and `agent.decide` carries `decide.blocked` / `decide.block_reason=already_charged`. Fail open: any other mode, a shadow game, an unknown mode or an unreadable `game` set (not READY) means no block; an unreadable set logs `agent.shield_charge_mode_unreadable` at most once per 30 s. The ledger is the agent's own record (no shield-state gauge, in memory only, lost on an agent restart), so it also refuses a grant after the player activated, which the coordinator would accept (UC-040 A31, BR-020), by design. The coordinator stays authoritative. The ledger also feeds the decide state (`shield_charged`, §1.2) and the rules engine, which drops a `grant_shield` candidate to a `shield_charged` target before ranking (`applyChargePolicy`, like the battery policy; the rules path takes one ledger snapshot per ranking and hands the same snapshot to the state it builds). An activation, an expiry or a death does not shorten the window: it is a pacing rule (#1870).
   - `shield_cap_reached` and `shield_cooldown` (#2044; UC-040 A33, BR-022), in every mode and game kind, from the live `policy.max_shields_per_player_per_game` (default 3, 0 = unlimited) and `policy.shield_cooldown_seconds` (default 15, 0 = off) of the cycle's snapshot (a read error or a negative value gives the default; flags.md §3). The cap counts the player's written `grant_shield`s in this game plus the ones in flight, so concurrent cycles cannot both take the last slot. The cooldown refuses while now is before the last shield's end plus the cooldown; the end is write time + granted seconds (the decision's value; a grant without one is stamped before this step with the `shield_seconds` `default` variant, 5 when unreadable, UC-040 BR-029, #2395) + the same 2 s margin, in trigger mode too (the cooldown runs from the grant, BR-022). While a cooldown is set, a grant still in flight for the player also refuses `shield_cooldown`: it has no end yet. The count is an upper bound of the coordinator's (BR-022): it also counts a write the coordinator refused or kept. 0/0 refuses nothing (the zero `Policy` of a hand-built snapshot, so the rules golden is unchanged). Repeats are quiet as for `already_charged`: the first refusal per reason since the last reservation or write is emitted in full, later ones only count in `agent_decisions_total` and log `agent.decision_shield_cap_reached_repeat` / `agent.decision_shield_cooldown_repeat` at debug level. The rules engine's no-shield filter (`applyChargePolicy`) is fed with the charged players plus the capped and cooling-down ones (`Loop.ShieldSerials`, one snapshot per ranking), so R4 does not propose a refused shield. R4's target is the weakest of the alive players with the fewest agent shields in this game (`Loop.ShieldGrants`: written plus in flight, the same per-game ledger; catch-up, UC-040 A41, BR-030, #2395), so the rules path's `ffa.target` answer and its decisions shield every alive player once before any twice; the decide state marks the charged ones `shield_charged` and the capped and cooling-down ones `shield_limited` (§1.2; #2066).
   - Hold games (#2096; UC-040 A37 step 1), in a game whose `game_shield_activation_mode` gauge says `hold` only, after the allow-list and battery and before the cap and cooldown: `already_charged` does not apply and `shield.activation` is not read from flagd (a hold game is latched, so a mid-game flip to `trigger` gives no charge window); `shield_budget_exhausted` when the player's budget remaining is below the minimum burst, then `shield_pool_full` when energy plus the seconds of this player's grants in flight is at least the full level `min(cap, budget remaining)` minus 0.5 s (a constant, C-035). The full level is the coordinator's (A37), but the margin is deliberately stricter than the coordinator's refusal, which needs less than 0.05 s of room: the agent also refuses a grant that could add 0.05 to 0.5 s, which the coordinator would accept (the safe direction: fewer shields, never a grant the coordinator refuses for a stale view). For a player whose shield is active only the pool check is skipped (the energy gauge over-reads a draining pool); the budget check stays (a draining budget only falls, so refusing early is safe). Both fail open when the gauges are missing (an agent restart, a collector outage): the coordinator stays authoritative and refunds its own refusal. In a game that is not latched to hold the trigger/instant read stays per grant from flagd, as the coordinator's. Like `already_charged`, the first refusal per entry gets the full spans, the warning and a timeline entry, repeats log `agent.decision_shield_pool_full_repeat` / `agent.decision_shield_budget_exhausted_repeat` at debug. `Loop.ShieldSerials(c)` adds such players to `noShield`, so `applyChargePolicy` drops their R4 candidates (`decision/chargeledger.go` `holdPoolOf`, `decide.HoldShieldRefusal`; tests `decision/chargeledger_hold_uc040_test.go`).
5. weighted per-game rate limit `policy.max_interventions_per_minute` → `rate_limit` (budget charged only on admit)

Budget-aware admission (#1326, UC-040 A20/A21, BR-014/BR-015; `decision/budget.go`, `decision/ratelimit.go`):

- A cycle's decisions reach the rate limit in rank order. The first decision that passes steps 1-4 but does
  not fit the remaining budget ends admission for the cycle: every lower-ranked decision is blocked
  `rate_limit` without charge. A decision costing more than the whole budget never fits and does not end
  admission. When the budget covers every decision, admission is unchanged.
- `send_controller_effect` is admitted only if one medium cost (1.0) stays free after it, unless `chaos` is
  the strictly dominant objective weight or the allow-list permits no protective intervention
  (`grant_shield`, `toughness_boost`, `adjust_player_sensitivity`, `set_player_handicap`). `play_audio_cue`
  and `adjust_volume` (also 0.5) keep no reserve (accepted, UC-040 BR-015).
- A `rate_limit` refusal is emitted in full (`agent.decision` + `agent.action`, warning, timeline) once per
  budget state per (intervention, target); the state changes when an intervention is admitted or one leaves
  the 60 s window. Repeats in between only count in `agent_decisions_total` and log
  `agent.decision_rate_limit_repeat` at debug level. `agent.decide` still reports `decide.blocked`. The
  dashboard's blocked tallies read `agent_decisions_total` for that reason (`services/dashboard/src/agentOverlay.ts`).

Blocked decisions are recorded on the `agent.decision` span
(`decision.blocked=true`, `decision.block_reason`) and never applied. The `agent.decide` span of the same
decision repeats the verdict as `decide.blocked` / `decide.block_reason` (§6, #1295).

## 5. Confidence and routing

- **Meaning:** the backend's confidence that *acting* on the answer is right, in
  [0,1]; derivation is backend-defined (`decide.go` (package doc)).
- **Rules:** softmax over ranked candidate scores with `noop` competing at
  `minScore` 0.10, temperature = `minScore`; `ffa.intervention` confidence is
  1 − p(noop) when an intervention is chosen, p(noop) when `noop` is chosen
  (`services/agent/decision/decide_rules.go:21-42,147-229`).
- **Rules path answers = the emitted decision (#1295).** In the decision loop the rules answers describe
  the decision the engine actually emitted this cycle (its first decision: intervention and target), from
  the same ranking. The `ffa.intervention` / `ffa.target` options of the rules-path request are the
  allow-listed interventions and alive players **plus** every intervention and target the engine ranked
  (`services/agent/decision/decide_rules.go:emissionRequest`), so `decide.questions` on a rules span can list an intervention
  the allow-list does not hold; the permission chain (§4.5) applies the allow-list afterwards and its verdict
  is `decide.blocked`. A remote backend's request, and `RulesBackend.Decide`, keep the allow-listed options.
  Only telemetry changed: the emitted decisions and their outcomes are pinned by
  `decision/testdata/rules_path_golden.json`. Because the softmax runs over the widened options, the rules-path
  `decide.confidence` (1 − p(noop) for the chosen intervention) is computed over the engine's own ranked
  interventions, not only the allow-listed ones.
- **Remote:** the model reports `confidence` per answer.
- **Routing (remote path only)** uses the `ffa.intervention` confidence against the
  **effective threshold** for the backend that answered (`flags.DecisionBackend.ActThreshold`,
  `services/agent/flags/decision_backend.go:DecisionBackend.ActThreshold`; #1287):
  - backend `jev`: `decision.jev.act_confidence_threshold` (default variant `gate` 0.6; variants
    inherit −1 / gate 0.6 / default 0.8 / strict 0.95). Jev's confidence is the calibrated gate p_yes (§3.1),
    not a self-report, so it has its own bar. The `inherit` sentinel (−1), and NaN or any other value outside
    [0,1], use the global flag instead; a flagd error yields 0.6 (the defaultVariant);
  - every other backend (claude, openai): the global `decision.act_confidence_threshold` (default 0.8,
    variants low 0.5 / gate 0.6 / strict 0.95; NaN or out of range → default).

  Both flags are read **live at landing** (`services/agent/decision/decide_hook.go:runDecideRemote`, `decideRemoteApply`), so flipping either mid-flight applies to
  the in-flight answer: ≥ threshold → `act`; < threshold → `held` (no action). The span records the effective
  threshold and its source (§12). The rules path does not route by confidence.
- `held` answers are not queued anywhere on `main` (the epic #1225 body proposed a
  shadow-candidate queue; not implemented).

## 6. Outcomes

`decide.outcome` on the `agent.decide` span (`services/agent/decision/decide_hook.go:110-127`):

| Outcome | Meaning |
|---|---|
| `act` | Routed to the permission chain (which may still block: `decide.blocked`, and the `agent.decision` span). Also the rules-path outcome when the rules emitted a decision. |
| `held` | Valid intervention answer below the threshold. No action. |
| `noop` | Backend chose `noop`. No action. Not seen on the rules path: a rules cycle whose ranking emitted no decision records no `agent.decide` span at all (`OnEvaluate` returns before any span); the rules record still sets `noop` for that case defensively, so a `noop` choice never reads `act` (#1295). |
| `invalid` | Cross-question inconsistency (4.3). No action, no fallback. |
| `fallback` | The requested backend could not decide; rules did. |
| `discarded` | A valid answer was dropped when it landed; see `decide.discarded_reason`. |

`decide.outcome` is the **routing** outcome. What the permission chain then did with the decision the
answers describe (the first decision of the cycle) is on `decide.blocked` (bool) and, when blocked,
`decide.block_reason` (`not_allowed`, `battery_threshold`, `experiment_flag`, `already_applied`, `already_charged`, `shield_budget_exhausted`, `shield_pool_full`, `shield_cap_reached`, `shield_cooldown`, `rate_limit`), present exactly when that decision
reached the permission chain: outcome `act`, or `fallback` when the rules fallback emitted a decision (#1295).
So a rules decision blocked by the allow-list reads `outcome=act`, `decide.blocked=true`,
`decide.block_reason=not_allowed`, with answers naming the blocked intervention and target. `already_applied` (§4.5 step 3, UC-040 A35) is not a refusal by a gate: the decided value is already in place, so nothing needed writing; it uses `blocked` so the closed outcome vocabulary stays as it is (`noop` keeps meaning "the backend chose noop").

`decide.discarded_reason`: `stale_context`, `game_ended`, `target_gone`, `state_too_old` (#2123),
`kill_switch`, `backend_changed`, `shutdown` (`services/agent/decision/telemetry.go:DiscardStateTooOld`;
`services/agent/decision/decide_hook.go:DecideDiscardBackendChanged`).

Outcomes are recorded **only as span attributes** (the dashboard Agent tab reads them from Jaeger,
`services/dashboard/src/agentDecide.ts:parseDecideTraces`). One exception: since #2123 every remote `agent.decide` record that carries
a `decide.discarded_reason` is counted in `agent_decide_discarded_total{reason}`
(`services/agent/decision/metrics.go:newDecideDiscardedCounter`), so dropped answers show without Jaeger.

### 6.1 Prediction registration (planned, #2479)

A decision record that is not a `noop` and carries a prediction registers it with the agent's prediction book: every rules
record, the rules fallback record (outcome `fallback`), and every remote record whose outcome is `act` (including a decision blocked by the permission chain, A10, or dropped by the actuation gate, A11) or `held` (A5, which ends at step 9) after `mapAnswers`, or `discarded` only for `target_gone` and `state_too_old` (A7, step 10; a `stale_context`, `game_ended`, `backend_changed`, `kill_switch` or `shutdown` discard registers nothing, and neither does a game that ended or a stale context). Noops carry no prediction in
v1 (maintainer decision 2026-10-31; no restraint calibration). The prediction is stamped `acted` = the permission chain passed
and the intervention was applied; a held, blocked or discarded one is kept as a counterfactual (`acted=false`) and reported
apart. An identical (backend, source, rule or lever, template, target, `acted`) prediction that is still pending is counted `merged` (`decide.prediction.merged=true` on `agent.decide`, `agent_prediction_merged_total`) and not
registered again. Registration, scoring and the controls are observation only: they never reach the permission chain, the
allow-list, the budgets or the writer (UC-040 BR-034). Details: §14.3.

## 7. Fallback reasons

`decide.fallback_reason` (constants in `decision/decide_registry.go` and `decision/telemetry.go`):

| Reason | When | Who decides |
|---|---|---|
| `unknown_backend` | `decision.backend` names no registered backend | rules, same cycle |
| `backend_unavailable` | no API key / model / base URL (`Available()==false`) | rules, same cycle |
| `async_not_wired` | no context provider (never in production) | rules, same cycle |
| `llm_not_eligible` | game kind not in `llm.eligible_game_kinds` (default `real`) | rules, same cycle |
| `llm_budget_exhausted` | global per-minute budget full | rules, same cycle |
| `timeout` | `decision.<backend>.timeout_ms` elapsed | rules on the **current** context when the call returns |
| `invalid_answer` | parse / validate failure, refusal, max tokens, other stop reasons | rules on the current context |
| `error` | anything else: transport, HTTP 4xx / 5xx **including 429**, error envelope | rules on the current context |

There is no separate `rate_limited` reason: HTTP 429 is `error`, with
`error.type="429"` on the `chat {model}` span.

The reason is recorded as `decide.fallback_reason` with `decide.outcome=fallback` on the `agent.decide`
span. Every same-cycle fallback is counted in `agent_llm_gated_total{reason}` on every cycle, decided or
not (`unknown_backend`, `backend_unavailable` and `async_not_wired` since #1377). When the rules emit no decision
in that cycle (#1350, `services/agent/decision/decision.go:OnEvaluate`, `services/agent/decision/decide_hook.go:emitQuietFallback`):

- the three **configuration** fallbacks (`unknown_backend`, `backend_unavailable`, `async_not_wired`;
  `services/agent/decision/decide_hook.go:isConfigFallback`) still emit an `agent.decide` span (`decide.outcome=fallback`,
  `decide.fallback_reason`, `decide.backend=rules`) under its own `agent.signal_received` root, throttled per
  reason to one per `decision.throttle_seconds` window;
- `llm_not_eligible` and `llm_budget_exhausted` stay counter-only, so quiet shadow games do not flood Jaeger;
- the log line `agent.decide.backend_misconfigured` fires on state changes only: WARN (`state=entered`,
  `reason`) when a game enters a configuration fallback or its reason changes, INFO (`state=cleared`,
  `previous_reason`) when it clears.

A decided cycle gets its `agent.decide` span every time, unthrottled.

**Not fallbacks** (rules stay quiet that cycle, no decision): the per-game cadence
gate (`decision.min_interval_ms` from the previous call's start and `decision.min_gap_after_answer_ms` from its end,
#2123; both `decide_interval`, counted in `agent_llm_gated_total{reason}`), a remote
call already in flight for that game (`services/agent/decision/decide_hook.go:decideRemote`), and an empty
allow-list (`no_interventions_allowed`, §7.1).

Order of checks on the remote path (`services/agent/decision/decide_hook.go:decideRemote`, `gateRemoteDecide`): in-flight claim →
resolve backend (unknown / unavailable) → async wired → eligibility → allow-list not empty →
cadence → budget → fire. There is no separate admission gate (see §8).

### 7.1 Call gate: no interventions allowed (#1298)

A remote decide call whose `ffa.intervention` question could only offer `noop` is
never made. `gateRemoteDecide` checks `hasAllowedInterventions(offeredInterventions(snapshot.InterventionsAllowed))` (the allow-list without the experiment-flag levers in an experiment game, §4.5 step 2a, #2251)
after eligibility and **before** the cadence and the global budget are charged and
before the request is built. The list counts as empty after the same filtering as
the `ffa.intervention` options (`services/agent/decide/ffa.go:interventionOptions`: duplicates,
empty ids and `noop` dropped), so the `none` variant (`""`) and the `probe` variant
(`"noop"`) both gate.

| Aspect | Behaviour |
|---|---|
| Remote call | none (no Jev / Claude / OpenAI request built or sent) |
| Cadence `decision.min_interval_ms` | not charged (`lastDecideAttempt` unchanged) |
| Gap `decision.min_gap_after_answer_ms` | not started (`lastDecideEnded` is set only when a call ends) |
| Global budget `llm.max_requests_per_minute` | not charged |
| Rules engine | not run: nothing it emits could pass the allow-list; not a fallback |
| `agent.decide` span | none (the cycle runs at 10 to 60 Hz; a span per cycle would be noise) |
| Metric | `agent_llm_gated_total{reason=no_interventions_allowed}` per gated cycle |
| Log | debug `agent.decide.no_interventions_allowed` only when the game enters (`skipping=true`) or leaves (`skipping=false`) the state, with `experiment_filtered=true` when the allow-list offered an intervention and only the experiment-flag filter (UC-040 A40 step 5) emptied it (#2251); a cycle on the rules backend counts as leaving |

Ineligible game kinds are checked first and still fall back to rules
(`llm_not_eligible`), so shadow games keep their rules-only behaviour. A backend
that cannot be resolved also still falls back to rules before this gate.

An answer already in flight when `interventions_allowed` flips to `none` is not
affected by this gate: the landing re-reads the live allow-list and the permission
chain blocks it with `not_allowed` (§9).

Scope: in-game decide calls only. The post-game retro (`llm.retro`) and the
experiment proposer do not read the allow-list for admission; they are governed by
the kill switch `enabled` and the shared LLM budget (NFR-011).

## 8. Timeouts, cadence and budgets

| Knob | Default | Scope | Source |
|---|---|---|---|
| `decision.claude.timeout_ms` | 5000 ms (variants fast 1500 / tight 2500 / default 5000 / patient 8000, PR #1257); the talk stage baseline runs `patient` (#2123, `scripts/talk-baseline.json`) | per call | `services/agent/flags/decision_backend.go:30,109-116,214` |
| `decision.openai.timeout_ms` / `decision.jev.timeout_ms` | 2500 ms (variants 1500 / 2500 / 5000) | per call | `services/agent/flags/decision_backend.go:33,37,219,226` |
| `decision.min_interval_ms` | 3000 ms (1500 / 10000; 0 disables) | per game, from admission (the previous call's start) | `services/agent/flags/decision_backend.go:DefaultDecisionMinIntervalMs`, `services/agent/decision/decide_hook.go:gateRemoteDecide` |
| `decision.min_gap_after_answer_ms` (#2123) | 1000 ms (off 0 / slow 3000) | per game, from when the previous call returned (an answer, a timeout or a failure), before the live flag re-read and any action write; set while the call still holds the in-flight slot, and a later discard does not move it. A call is admitted only when both this gap and `decision.min_interval_ms` have passed | `services/agent/flags/decision_backend.go:DefaultDecisionMinGapAfterAnswerMs`, `services/agent/decision/decide_hook.go:noteDecideEnded` |
| `decision.max_state_age_ms` (#2123) | 10000 ms (off 0 / tight 6000) | per answer that would act, read live at landing: older fire-time state → `discarded/state_too_old` (§4.4). The default sits above the longest timeout (`patient` 8000 ms) so it only drops answers built on a state that was already stale at fire time | `services/agent/flags/decision_backend.go:DefaultDecisionMaxStateAgeMs`, `services/agent/decision/decide_hook.go:stateTooOld` |
| `llm.max_requests_per_minute` | 6 (conservative 3 / aggressive 12 / `stage` 30, PR #1257) | **global**, shared by in-game decisions, the retro and the proposer (PR #1284) | `services/agent/flags/flags.go:43`; `services/agent/decision/llmbudget.go:22-67` |
| `AGENT_LLM_MAX_REQUESTS_PER_MINUTE` (env) | unset | read once at startup; a positive value is a ceiling: the cap is `min(env, flag)`, so a flag can only tighten it (#1788, C-034) | `services/agent/tuning.go:llmBudgetCeiling`, `services/agent/decision/llmbudget.go:ceilBudget` |
| `llm.eligible_game_kinds` | `real` | which game kinds may call a remote backend; read by in-game decisions, the retro and the proposer | `services/agent/flags/flags.go:llmGate` |
| `policy.max_interventions_per_minute` | 2, weighted (soft 0.5, medium 1, hard 2, unknown 1); admitted in rank order with a 1.0 reserve against nudges (§4.5, #1326); talk stage `aggressive` 4 | per game | `decision/ratelimit.go`, `decision/budget.go` |
| `policy.coordinator_backstop_per_minute` | 60 | global, enforced in the game coordinator | `services/game_coordinator/interventions.py:DEFAULT_COORDINATOR_BACKSTOP_BUDGET`, `_refresh_budget` |
| Rules engine cadence | evaluate ≤ 1/s, ≤ 2 decisions per evaluation | per game | `services/agent/decision/rules.go:15-26` |
| Shared HTTP client backstop | 30 s | process | `services/agent/decide_backends.go:56-62` |

**Length gate (#1207) is retired.** #1207 introduced a rules-first architecture
where the LLM was a length-gated async upgrade
(`expected_seconds = players × llm.seconds_per_player × llm.style_factor`). PR #1252 (the change for issue
#1234, which removed the LiteLLM gateway and the legacy in-game LLM path) removed it from the code; none of
those flags exist on `main`.

**Budget fit at the stage baseline (#2123).** The stage keeps `decision.min_interval_ms` at its 3000 ms default
and runs Claude with the `patient` timeout. One game asks at most once per `max(min_interval, call latency + gap)`,
with the gap counted from when the answer lands: Jev (≈ 0.3 s answers, the main stage backend) every 3 s, about
20 calls a minute; warm Claude (≈ 3 s) every 4 s, about 15; a Claude call that runs into the 8 s `patient`
timeout every 9 s, about 6 to 7. Each fits the `stage` budget of 30 a minute with room for the post-game retro;
a second concurrent remote game on Jev can reach it (`llm_budget_exhausted` → rules, never a stall). A shorter
interval (the `fast` 1500 ms variant) would let Jev ask about 40 a minute, fill the budget during play and gate
the retro, so the stage does not use it. The longer timeout only lowers demand: a call in flight holds the
game's slot, so a slow answer is fewer calls, not more.

There is no game-length admission gate **by design** (#1231): the cadence,
the global budget and the per-game in-flight guard are the gates (`services/agent/decision/decide_hook.go:gateRemoteDecide`). The
always-true `remoteDecideAdmitted` seam that stood in for the length gate was removed in the #1322 cleanup.

### 8.1 Measured latencies (cited)

| Backend | Measurement | Source |
|---|---|---|
| Claude Haiku 4.5 | 5.55 s cold / 3.43 s warm (2-player FFA, dev machine) | #1231 comment 2026-10-16 |
| Claude Sonnet 5.5 | 3.65 s cold / 3.05 s warm | #1231 comment 2026-10-16 |
| Claude Haiku 4.5 after PR #1257 | ≈ 2.7 to 3.0 s warm | PR #1257 body |
| Claude Sonnet 5.5 after PR #1257 | ≈ 2.85 s warm | PR #1257 body |
| Jev 1.13.0, single call | 240 to 315 ms (3 questions, 13 interventions), ≈ 250 ms typical | PR #1261 body |
| Jev two-stage round | noop round (1 call) 228 to 322 ms; act round (2 calls) 469 ms warm, 525 ms cold | PR #1265 body |
| Jev gated round (#1281) | gated stage 1 (gate + intervention, 1 call) 219 to 336 ms over 16 samples (one 910 ms outlier); act round (2 calls) 473 ms warm, 567 ms cold | #1281 PR body |

The first two rows predate PR #1257 and exceeded the then-default 2500 ms timeout. Since PR #1257 the Claude
default is 5000 ms, which warm calls fit (resolved discrepancy D-K6); cold calls can still exceed it.
No p50 / p95 from production `agent.decide` spans has been published yet (#1231).

## 9. Kill switch, gates and live changes mid-flight

| Change while a remote call is in flight | Effect | Evidence |
|---|---|---|
| `enabled` → off | HTTP call is **not** cancelled; on landing the live flags are re-read and the answer is `discarded/kill_switch`; the rules fallback is suppressed too | `services/agent/decision/decide_hook.go:runDecideRemote` (landing re-read); `TestRemote_KillSwitchAtLanding`, `TestRemote_KillSwitchSuppressesTimeoutFallback`; service tier `tests/service/test_tc009_kill_switch.py::test_kill_switch_discards_inflight_answer` |
| `decision.backend` changed | `discarded/backend_changed` | `services/agent/decision/decide_hook.go:runDecideRemote`; `TestRemote_BackendChangedMidFlight` |
| `decision.act_confidence_threshold` / `decision.jev.act_confidence_threshold` changed | the effective threshold for the answering backend is re-read at landing (§5); the answer is `act` or `held` against the new value | `services/agent/decision/decide_hook.go:runDecideRemote`; `TestRemote_ThresholdRaisedMidFlight`, `TestThreshold_JevRaisedMidFlightAppliesAtLanding`, `TestThreshold_JevInheritFlippedMidFlightAppliesAtLanding` |
| `interventions_allowed` narrowed (including to `none`) | outcome stays `act`; the permission chain blocks `not_allowed` (`decide.blocked=true`, `decide.block_reason=not_allowed`); the next cycle makes no new call (§7.1) | `TestRemote_AllowListAtLanding`, `TestRemote_BlockedDecisionNamesTargetAndDecideBlocked`, `TestRemote_NoInterventionsAllowed`, `TestRemote_NoInterventionsAllowedAtLanding` |
| `interventions_enabled` → off | not checked by the decide hook; the gated sink writes nothing | `services/agent/actions/gated.go:GatedActionSink.Apply` |
| agent shutdown | `discarded/shutdown` | `services/agent/decision/decide_hook.go:runDecideRemote` (shutdown check first) |

With `enabled` off no new remote call fires: `OnEvaluate` short-circuits and emits a
throttled `agent.disabled` span (`services/agent/decision/decision.go:OnEvaluate`).

## 10. Live backend switch

`decision.*` flags are evaluated on every cycle, never cached
(`services/agent/flags/decision_backend.go:10-23`). The registry factory runs per remote call with
that cycle's snapshot, so model, base URL and timeout changes apply to the next call
(`services/agent/decision/decide_registry.go:14-20`). Unknown names are kept verbatim on the span;
an empty value means `rules`.

`scripts/agent-backend.sh` sets stack overlays on the flag service (writer `stage`,
[UC-053](../use_cases/UC-053.md) A1): `status`, `{rules|claude|openai|jev} [model]`,
`threshold {low|gate|default|strict}`, `capture {on|off}`, `effort {…}`. The flag service
pushes the change to flagd at once (NFR-050); the agent reads the flags at use time.

### 10.1 The text backend (post-game retro and proposer)

Not part of the decide contract, but it shares the key, the budget and the eligibility gate, so it is
specified here (#1234). The agent talks to models **directly**: there is no gateway or router (NORTHSTAR:
hosted inference is allowed, local inference is not a goal, rules are the offline floor). The LiteLLM gateway
(#1049) and the `mode` / `model` / `prompt_variant` flags were removed in #1234.

To enable the model-backed retro and proposer (the compose `agent` service has no profile, so a plain `up` starts it;
the old `--profile agent` flag selects nothing): give the agent the key, then set the flag (the flag is the only
switch, its env fallback was removed in #1907; the proposer additionally needs its gate, FR-152 d):

```bash
ANTHROPIC_API_KEY=<key> docker compose up -d agent
./scripts/flags.sh set agent inference.backend claude
```

| Seam | Used for | Selected by | Backends |
|---|---|---|---|
| Decision backend | in-game decisions (FFA) | `decision.backend` flag, live | `rules` (default), `jev`, `claude`, `openai` |
| Text backend | post-game retro (`agent.llm.retro`), experiment proposer | `inference.backend` flag, per use (retro / propose cycle); `stub` while flagd is unresolved (no env fallback, #1907) | `stub` (default), `claude` (needs the key and a valid base URL) |

The text backend takes a System + User prompt and returns raw text (`Infer(ctx, llm.Prompt) (string, error)`,
`inference/claude.go`, official Anthropic Go SDK). Env vars (`AGENT_INFERENCE_BASE_URL`, `ANTHROPIC_API_KEY`) are in the
agent env table in [services.md](services.md) section 6. Keys live in the agent environment only (compose passes
`ANTHROPIC_API_KEY`, `DECISION_OPENAI_API_KEY`, `JEV_API_KEY`); a backend without its key is unavailable and
decisions fall back to rules (§7). Keep keys out of the tracked `.env`: export them or use the git-ignored
`.env.local` (`docker compose --env-file .env --env-file .env.local up`).

- **Stub** (default, or `claude` without a key, which logs a warning): the proposer proposes nothing, the retro is
  capture-only (`inference.used=none`, `inference.fallback_reason=no_backend_available`).
- **Retro**: calls the model only for game kinds in `llm.eligible_game_kinds` (default `real`); a shadow-game
  retro stays capture-only (`inference.fallback_reason=llm_not_eligible`). A model call draws one slot of the
  shared `llm.max_requests_per_minute` budget (§8). With `enabled` off the retro is capture-only
  (`inference.fallback_reason=agent_disabled`): no model call, no slot (#1270).
- **Proposer**: runs on every game end while `enabled` and `experiments_enabled` are both on (read at each game
  end; `enabled` fails closed when flagd is unreachable) and checks both again just before writing the
  experiment value to the flag service, so switching the agent off during a model call still blocks the write.
  With `claude`, a proposal that passes the cadence floor (`code_improvement.min_interval_seconds`) calls the
  model only for eligible game kinds and only when a budget slot is free; otherwise it records
  `agent_llm_gated_total{reason=llm_not_eligible|llm_budget_exhausted}` and retries at the next game end (#1270).
  The stub never draws from the budget. The `inference.backend` flag selects the backend per cycle (`code_improvement.engine`
  was retired in #1810; a stub cycle is never charged). The proposer runs at all only while the proposer gate is open:
  `AGENT_EXPERIMENT_PROPOSER_ENABLED=true` AND `experiment_proposer_enabled` (FR-152 d, C-034); closed, it makes no
  model call and draws no budget slot.
- **The budget is tight during a remote-decide session.** The window is a sliding 60 s shared by decisions, the
  retro and the proposer. With a remote `decision.backend`, `decision.min_interval_ms` 3000 and
  `decision.min_gap_after_answer_ms` 1000 one game wants about 15 to 20 calls a minute against the default cap of 6, so at game end the retro and proposer usually find it
  full (`llm_budget_exhausted`). For retros or proposals in such a session use the `stage` cap
  (`llm.max_requests_per_minute` = 30) or `decision.backend=rules`.
- **Shadow-only runs make no proposals** with the default `llm.eligible_game_kinds = real`. `real_and_shadow`
  allows them but also lets shadow games use remote decide calls and retros.
- On models that accept it (Opus 5.5, Opus 5, Sonnet 5.5, Fable 5.1) the client sends `fallbacks: "default"`
  (beta `server-side-fallback-2026-07-01`), so a policy refusal is re-run on a fallback model inside the same call.
- Telemetry: every model call is one GenAI-semconv `chat {model}` CLIENT span (§12), nested under `agent.decide`
  or `agent.llm.retro`, with W3C `traceparent` injected. Failures record only a low-cardinality `error.type`;
  vendor error text never reaches a span (#1242).

## 11. From decision to game effect

The action sink writes to the flag service's `interventions` set, always for the deciding
game (`actions/writer.go`, gated by `interventions_enabled`; [UC-053](../use_cases/UC-053.md)):
a state value with `PUT … scope {game_id}`, a per-player value with
`scope {game_id, serial}`, a one-shot event with `POST` (appended to the game's event list).
A decision without a coordinator game id (`game_…`), such as the `session-<n>` id of the
agent's partition for unlabeled signals, is not written (no flag-service call, no retry) and is recorded as a failed write
(`agent_intervention_writes_total{result=error}`, `agent.action` span error; #1323, #1651); a `game_not_live` refusal is retried
once after 250 ms. The game coordinator applies the result
(`services/game_coordinator/interventions.py`). Full flag shapes and bounds:
[flags.md § interventions](flags.md#5-interventions), [§13.1](flags.md#131-interventions-all-writes-are-game-scoped).

| Intervention | Flag | Shape | Writer default |
|---|---|---|---|
| `play_audio_cue` | `audio_cue` | event, payload `{"sound"}` | `agent_cue` (an audio-registry alias of the lobby ready ping `beep_loud`; UC-040 BR-007) |
| `send_controller_effect` | `controller_effect` | event, payload `{"serial","effect"}` | `rumble` |
| `eliminate_player` / `revive_player` | same name | event, payload `{"serial"}` | |
| `end_game` | `end_game` | event, payload `{}` (the deciding game is the scope) | |
| `adjust_music_tempo` | `music_tempo_override` | state | 1.15 |
| `adjust_volume` | `volume_override` | state | 0.7 |
| `adjust_global_sensitivity` | `global_sensitivity_override` | state | 2 |
| `adjust_global_difficulty` | `global_difficulty_factor` | state | 1.5 |
| `set_pacing_profile` | `pacing_profile` | state | `frantic` |
| `ramp_tempo` | `tempo_ramp` | state | `1.3:8:linear` (`<target 1.0 to 1.3>:<seconds, at most 15>:<linear\|ease>`; the coordinator caps a longer curve at 15 s and the agent writer sends no value above 15 s, #1840) |
| `auto_rubberband` | `auto_rubberband` | state | `gentle` |
| `adjust_player_sensitivity` | `player_sensitivity_factor` | per serial level | 1.5 |
| `set_player_handicap` | `player_handicap_factor` | per serial level | 1.25 |
| `grant_shield` | `shield_seconds` | per serial grant | the value of the `default` variant of `shield_seconds`, read at use time through the flag service and stamped on the decision by the loop; 5 when it cannot be read or is outside 1 to 10 (UC-040 BR-029, #2395) (feedback: sound `shield_up` + `GAME_EFFECT_SHIELD`, UC-040 A16). #1837: in trigger mode (`shield.activation`, UC-040 BR-019; Free-for-All only) the grant is a **charge** (`GAME_EFFECT_SHIELD_CHARGE`, UC-040 A30): the seconds are the protection the player gets when they activate it with the trigger (UC-030 A6). The coordinator may block it `already_charged` (A31) |
| `toughness_boost` | `toughness_boost_seconds` | per serial grant | `5:2.0` (feedback: `shield_up` at half volume + `GAME_EFFECT_TOUGHNESS_BOOST`) |
| `soft_penalty` | `soft_penalty_action` | per serial grant | `warn` |
| `noop` | (no write) | | |

Every value is rendered by the flag service as targeting on the game's `gameId` (and the
player's serial), so it resolves in no other game, and it is dropped when the game ends
(FR-136, [UC-053](../use_cases/UC-053.md) BR-003). The coordinator evaluates every
intervention flag with `{targetingKey=serial, gameId}`.

**Events** (`eliminate_player`, `revive_player`, `audio_cue`, `controller_effect`,
`end_game`) resolve, for one game, to its event list `{"epoch","seq","events":[last 16]}`. The
coordinator applies every event with a sequence number above the last one it applied for that
game and flag, in order, exactly once; the last applied `(epoch, seq)` advances before the
events are dispatched; a read that returns an evaluation error applies nothing; only a new,
non-empty epoch (a flag-service restart) restarts the count; more than 16 events between two
reads is logged and counted (`game_intervention_event_gaps_total`)
([UC-040 BR-006](../use_cases/UC-040.md#br-006-a-one-shot-intervention-fires-once-only-in-its-game), FR-137).
`end_game` force-ends only the session whose list holds it (`_handle_end_game` →
`end_game_fn(game_id, reason)` = `GameCoordinatorServicer._force_end_session`, the same path as
`ForceEndGame` with a `game_id`) ([UC-040 BR-011](../use_cases/UC-040.md#br-011-end_game-ends-only-the-game-it-was-decided-for), #1310).

**Group shield event** (planned, #2467 PR 3; the flag and its registry row land with the coordinator handler): `group_shield` is a sixth event flag with the payload
`{"serials":"A,B,C","seconds":"5"}` (`serials` 2 to 6 distinct players of the game, `seconds` 1 to 10; the flag service
checks both, [flags.md §13.1](flags.md#131-interventions-all-writes-are-game-scoped)). Like every event it is applied at
most once per event seq: the seq advances before dispatch, so a failed dispatch is not retried, and an agent restart
resets the agent's own counter, which is why the coordinator keeps its own per-game count
([UC-040 BR-033](../use_cases/UC-040.md)). No `interventions_allowed` variant names `grant_group_shield` and no agent code
writes the event until the PRs that add them; `tests/contract/test_flagd_resolution.py`
`test_allow_list_variants_only_name_coordinator_types` keeps a type the coordinator cannot apply out of every variant.

Per-serial **grant** flags (`shield_seconds`, `toughness_boost_seconds`,
`soft_penalty_action`) are one-shot per decision: every write gets a fresh variant from the
flag service. The coordinator (`InterventionManager._evaluate_targeted_grant`) reads each
player's variant and value with evaluation details and dispatches one intervention per player
whose variant or value changed during the game and is not neutral, with `target` = that serial.
An error read never moves that player's last-seen grant. Two grants to one player in one pushed
configuration collapse to the later one. Per-serial **level** flags
(`player_sensitivity_factor`, `player_handicap_factor`) keep the last-seen level per alive,
battery-eligible player; on a change the coordinator dispatches only the players whose level
differs from the last read in this game: players moving to neutral go to the flag's revert
handler (factor back to 1.0, no permission chain, so a blocked chain cannot leave a factor
stuck), the others go through the chain as a `{serial: level}` value the handler applies
([UC-040 BR-017](../use_cases/UC-040.md#br-017-per-player-levels-belong-to-one-game), A24, #1317).
No per-player entry outlives its game, so the agent prunes nothing
([UC-040 BR-016](../use_cases/UC-040.md#br-016-per-player-entries-live-only-as-long-as-their-game)).

The coordinator re-checks independently before any handler runs
(`InterventionManager._enforce_and_dispatch` → `_check_chain` in `interventions.py`): allow-list membership → mode capability deny matrix
(`MODE_CAPABILITY_DENY` in `interventions.py`) → battery guard → global backstop. Its block reasons are
`not_allowed`, `rate_limited`, `low_battery`, `mode_unsupported`, `no_game`, `already_charged` (a `grant_shield` to a player who holds a shield charge; the handler applies nothing and the backstop reservation is refunded; UC-040 A31, #1837), `shield_cap_reached` and `shield_cooldown` (a `grant_shield` refused by the coordinator's shield limits per player per game; nothing applied, reservation refunded; UC-040 A34, BR-022, #2044), `shield_pool_full` and `shield_budget_exhausted` (a hold-mode `grant_shield` refused because the player's energy pool is full or the game's shield budget is used up; nothing applied, reservation refunded, not counted against BR-022; UC-040 A37, #2096), `game_ending` (a `grant_shield` that lands once the game is ending: outcome latched, force-ended, or state ENDING / ENDED; nothing applied, no shield layer and no cue, reservation refunded; UC-040 A42 step 3, #2395), `revive_refused` (the mode refused a `revive_player`; nothing applied, reservation refunded; UC-040 A22, #1948), `eliminate_refused` (an `eliminate_player` that killed nobody: unknown or already-dead player, a kill in flight, or the mode declined it; nothing applied, reservation refunded; UC-040 A22, #1383), `experiment_override` (a `pacing_profile` in an experimental-arm game whose experiment sets `windows` to another value; the experiment's windows stay, reservation refunded; UC-043 BR-005, #1970), plus
`handler_error`, `game_loop_unavailable` and `game_loop_timeout` (the handler did not run on the game's own
event loop: not applied), plus `game_loop_late` on a not-blocked outcome (the handler started but had not finished
after 5 s: outcome unknown; [UC-040 BR-018](../use_cases/UC-040.md#br-018-intervention-side-effects-run-on-the-owning-games-event-loop), #1250). Every outcome emits `game_interventions_total{type,objective,blocked,block_reason}`
and an `agent_intervention` game event. An applied player-targeted intervention is also recorded as an event on
that player's `player_lifecycle` span naming the player (`player.serial`, `player.name`; event names in
[telemetry.md §3.2](telemetry.md#32-game-coordinator), #1295).

Every payload the writer can send must be playable or visible on the stack it targets: the writer
default sound id resolves in the audio registry (`services/audio/sound_catalog.py`, tested by
`services/audio/tests/test_sound_registry.py`), and the audio service logs a WARNING for an id it does
not know (UC-040 BR-007, #1301).

## 12. Telemetry

Full names in [telemetry.md](telemetry.md#4-agent). Summary:

- `agent.decide` (INTERNAL). Rules path: sibling of `agent.decision` under
  `agent.signal_received`, only when the cycle produced decisions; ended after the cycle's decisions ran
  (backdated to the ranking) so it can carry their permission verdict. Remote path: async
  root parented on the game's `game_cycle` span (fallback `gameplay_phase`, then the game root; UC-040 BR-013), linked to the fire-cycle
  `agent.signal_received` (`link.kind=fire_cycle`); parent of the backend's
  `chat {model}` CLIENT span and of the resulting `agent.decision`.
  Always: `gen_ai.agent.name=joustmania-agent`, `gen_ai.conversation.id=<game id>`,
  `game.id`, `session.id`, `game.kind`, `decide.backend`, `decide.backend.requested`,
  `decide.fallback_reason`, `decide.state.digest`, `decide.questions`,
  `decide.latency_ms`, `decide.outcome`. Conditional: `decide.act_confidence_threshold` (the EFFECTIVE
  threshold, §5) and `decide.act_confidence_threshold.source` (`jev` = `decision.jev.act_confidence_threshold`,
  `global` = `decision.act_confidence_threshold`; #1287), both only when routed,
  `gen_ai.request.model`, `decide.answers`, `decide.confidence`, `decide.confidence.min`,
  `decide.rationale`, `decide.invalid_reason`, `decide.discarded_reason`, `error.type`, `decide.blocked` and
  `decide.block_reason` (§6; #1295).
  Event `gen_ai.evaluation.result` (`gen_ai.evaluation.name=act_confidence_threshold`,
  score value, label `pass`/`fail`, explanation `confidence 0.720 >= threshold 0.600
  (decision.jev.act_confidence_threshold)` naming the flag the threshold came from) when routed
  (`services/agent/decision/decide_hook.go:emitDecideSpan`, `setDecideAttributes`).
- **`decide.prompt_profile`** (#2467 PR 5): on a remote-path `agent.decide` span, the normalized profile the request was
  rendered with (`v1` or `neutral`, §3); absent on the rules path.
- **Prediction attributes (planned, #2479).** On `agent.decide`, when the record carries a prediction:
  `decide.prediction.id` (uuid), `decide.prediction.template`, `decide.prediction.characteristic`,
  `decide.prediction.scope`, `decide.prediction.horizon_s`, `decide.prediction.p`, `decide.prediction.source`
  (`rules`, `lever_default`, `model`) and `decide.prediction.rule` (rules `R1`..`R9`, `R4d`, or the lever name);
  `decide.prediction.dropped_reason` when a model prediction was dropped (§4.1); `decide.prediction.merged=true` when an identical prediction was already pending (§6.1); `decide.goal.profile` when a goal line
  was rendered into the prompt (§14.4). The scored result is a separate span, `agent.prediction.outcome`
  ([telemetry.md §4.1](telemetry.md#41-spans-and-topology)), linked to this span.
- **`decide.answers`** is the JSON array of `decide.Answer` values (capped at the span-JSON size limit, with a
  truncation marker), one object per question:
  `{"question_id": "ffa.intervention", "choice": "grant_shield", "probabilities": {"noop": 0.3, "grant_shield": 0.7}, "score": 0, "confidence": 0.7}`,
  `{"question_id": "ffa.target", "choice": "MOCK0003", ...}`, `{"question_id": "ffa.magnitude", "score": 0.25, "confidence": 0.4}`;
  `choice`, `probabilities` and `noul` are omitted when empty. On the rules path the `ffa.intervention` /
  `ffa.target` choices are the emitted decision's (§5), also when the permission chain blocks it. Consumers: the dashboard Agent tab reads
  `question_id` / `choice` for `ffa.intervention` and `ffa.target` (`services/dashboard/src/agentDecide.ts:parseDecideAnswers`), and the
  decide service-tier tests match the scripted intervention in it (`tests/service/test_tc008_remote_backend_routing.py`, `tests/service/test_tc009_kill_switch.py`).
  Changing this shape breaks both.
- `chat {model}` (CLIENT) per remote call: OTel GenAI semantic conventions
  (`gen_ai.operation.name=chat`, `gen_ai.provider.name`, request / response model,
  token usage, finish reasons, `error.type`; jev adds `jev.native_confidence` and `jev.stage`, one span per stage, and on a gated stage 1 `jev.gate.p_yes`, `jev.stage1.choice`, `jev.stage1.distribution`). Prompt and completion content only when
  `decision.capture_content` is on (`decide/llmio/genai.go`).
- `agent.decision` / `agent.action` of a player-targeted decision carry `player.serial` (and `player.name`
  when known), [telemetry.md §4.3](telemetry.md#43-decision-attributes) (#1295).
- Metrics: `gen_ai.client.operation.duration`, `gen_ai.client.token.usage`,
  `agent_llm_gated_total{reason}` (decide path reasons: `llm_not_eligible`, `llm_budget_exhausted`,
  `unknown_backend`, `backend_unavailable`, `async_not_wired` (#1377), `decide_interval`, `no_interventions_allowed`), `agent_decisions_total`, `agent_interventions_applied_total`.

A ledger refusal (`already_charged`, `shield_cap_reached`, `shield_cooldown`, §4.5 step 4) or an `already_applied` suppression (§4.5 step 3) is not dispatched: no `agent.action` write, no effect sample, no budget.

`agent_interventions_applied_total` counts successful applies (written interventions and the probe
`noop`, which succeeds without a write): an apply that the
gated sink turns into a no-op while `interventions_enabled` is off is not counted
(`services/agent/actions/gated.go:GatedActionSink.ApplyReported`, #1681).

## 13. Changing this contract

A new question kind, outcome, fallback reason, discarded reason, span attribute or
default changes this file first (see [CONTRIBUTING](../CONTRIBUTING.md#spec-driven-development-aiup)).
The contract tests live in `decide/*_test.go`, the backend tests `decide/backends/claude/claude_test.go`,
`decide/backends/openai/openai_test.go` and `decide/backends/jev/contract_test.go` (only jev has a dedicated
`contract_test.go`; jev also has `stages_test.go` and `wire_test.go`), and `decision/decide_hook_test.go`
(per-backend threshold: `decision/decide_hook_threshold_test.go`; empty allow-list: `decision/decide_no_allowed_test.go`).

**Traceability:** FR-044, FR-045, FR-046, FR-047, FR-048, FR-049, FR-050, FR-051, FR-052, FR-053, FR-054, FR-200, FR-201 (planned, #2479) / UC-040, UC-044, UC-045 /
`services/agent/decide/` (`state.go`, `danger.go`, `hold.go`, `backends/jev/criteria.go`, `llmio/llmio.go`), `services/agent/decision/decide_hook.go`, `chargeledger.go`, `rules.go`, `decide_registry.go`, `decide_rules.go`, `decision.go`, `services/agent/decide_backends.go`, `services/agent/actions/`, `services/game_coordinator/interventions.py` /
`services/agent/decide/*_test.go`, `services/agent/decision/decide_hook_test.go`, `tests/service/test_tc008_remote_backend_routing.py`, `tests/service/test_tc009_kill_switch.py`, `tests/service/test_tc006_agent_blocked_by_host.py`, `tests/integration/test_tc013_shadow_alongside_real.py`.

## 14. Characteristics, predictions, goals (planned, #2479)

> Status `Open`: no code on `main` yet. Delivered in slices (characteristics and rules predictions, summary and retro,
> model ask, goal profiles). The maintainer decisions of 2026-10-31: `activity` is the movement intensity relative to the
> death threshold (always present, not the optional variance); noop predictions are not scored in v1; arm calibration is
> report-only. Requirements: FR-200, FR-201. Rules: UC-040 A44, A45, BR-034.

The agent observes the game only through telemetry (OTel-only observation) and acts only through flags. One small set of
**measurable characteristics** is the shared vocabulary of (a) predictions the agent makes about what its intervention
will do and (b) the human-chosen **goal profiles** a game is scored against. Both use the same measurement functions, so
calibration and goal attainment come from one pipeline. Neither reaches the permission chain, the allow-list, the budgets,
the writer or the rules weights: the worst a wrong prediction or goal can do is a misleading number or prompt line.

### 14.1 Characteristics

One pure `Measure(context, scope, window)` per characteristic, returning a value and an `ok` flag. A window needs samples for at least
half of its seconds and at least 2 players, otherwise the result is `data_missing`. Windows are at most 15 s so
the before and after windows both fit the 32-sample (about 32 s) movement history. A field prediction (`activity_rises` field,
`closeness_rises`) fixes its player set at registration: the alive players covered in the before window. The after window is
measured on the same set; when one of them is no longer alive it is `data_missing`, so a spread that shrank because the outlier
left is never a hit. Hard bounds of the characteristics are in §14.4.

| Characteristic | Definition | Signal | Scopes |
|---|---|---|---|
| `duration` | Game seconds elapsed, and whether the game ended (end reason zero or `root_span`; a stale end is no data) | `Session.DurationSeconds`, the game-end consumer | game |
| `activity` | Mean of (movement intensity / death threshold) over the window's movement samples (at least half of the window's distinct seconds must have a sample), for the players alive when it is measured; normalised, so tempo changes do not move the scale | `History`, `SpeedTrack.DeathThreshold`, `Session.DeathThreshold` | field, target |
| `change` | Swing of field `activity`: max minus min of the 5 s sub-window means inside the window | the same buckets | field |
| `elimination_pace` | Eliminations observed in (t, t+w], per minute; predictions use the count | `Timeline` elimination events (agent receipt time), `EliminationSequence` length as fallback | game |
| `survival` | The target is alive at t+w and was never eliminated in between | `EliminationSequence`, `Active` | target |
| `closeness` | Spread (max minus min) of per-player `activity` among alive players; lower means closer | `History` | field |

`tempo_followed` is not a characteristic: players adapting to tempo is `activity` rising after an `adjust_music_tempo`, and
the lever is recorded on the outcome (`activity|lever=adjust_music_tempo`). Elimination detection lags by one export
interval, so the horizon check fires at t + horizon + 3 s and judges an elimination by its timeline timestamp
(`prediction.timing=approx` when the timeline ring no longer holds it).

### 14.2 Prediction templates

Six closed templates with a closed set of horizons (so calibration stays comparable over time). The scope follows the template: `no_elimination`, `elimination` and `game_ends` are `game` scope; `closeness_rises` is `field`; `survives` is `target` (with `Target`); `activity_rises` is `target` when the decision has a target, otherwise `field`. All times are agent receipt time.

| Template | Characteristic | Hit / miss / unscorable | Horizons (s) |
|---|---|---|---|
| `survives` | `survival` (target) | Hit: alive at t+h. Miss: eliminated in (t, t+h], decided early. Unscorable: game ended first with the target alive, target not alive at registration, no target | 10, 15, 30 (default 15) |
| `no_elimination` | `elimination_pace` = 0 | Miss: any elimination in (t, t+h], early. Hit: none at t+h. Unscorable: the game ended without a new elimination | 10, 15, 30 |
| `elimination` | `elimination_pace` >= 1 | Hit: early. Miss: none at t+h. Unscorable: the game ended first without one | 15, 30, 60 |
| `activity_rises` | `activity` (field or target) | Hit: after > before x 1.05. Miss: otherwise. Unscorable: coverage too low, target died, game ended first | 10, 15 |
| `closeness_rises` | `closeness` | Hit: after-spread < before-spread x 0.95 | 15 |
| `game_ends` | `duration` | Hit: the game ends (zero or `root_span`) by t+h, early. Miss: still running at t+h. Unscorable: stale end | 30, 60, 120 |

Unscorable is never counted as a hit or a miss; it carries a reason (`game_ended`, `data_missing`, `stale_end`,
`overflow`, `no_target`, `target_not_alive`). A prediction that can be decided before the horizon (a miss of `survives` or
`no_elimination`, a hit of `elimination` or `game_ends`) is scored then (`prediction.decided_early`).

**Rules predictions** (a fixed table in the rules engine, stamped `Source=rules`; `P` is a fixed per-rule prior, documented
as a prior and not a claim; the value is in the table):

| Rule | Prediction |
|---|---|
| R1 slow music | none: the tempo effect on eliminations is confounded with the threshold change (the death threshold slides from SLOW_MAX to FAST_MAX with the tempo, so slowing makes the same motion more deadly); see #2487 |
| R2 calming cue | `no_elimination` 15, P 0.55 |
| R3 handicap outlier | `closeness_rises` 15, P 0.55 |
| R4 catch-up shield | `survives` 15, P 0.7 |
| R4d danger shield | `survives` 10, P 0.7 |
| R5 speed up | `elimination` 30, P 0.6 (kept as a prior, but the tempo-threshold confound applies here too: a faster tempo raises the death threshold, so eliminations can get rarer; scored by lift against the control, see #2487) |
| R6 eliminate least-moving | `game_ends` 60, P 0.5, only when at least 3 players stay alive after the action, otherwise none (trivially true) |
| R7 end game | none (the action is the outcome) |
| R8 statue rumble | `activity_rises` target 10, P 0.6 |
| R9 random effect | `activity_rises` target 10, P 0.6 |

**Lever defaults** (a pure mapping from the chosen intervention, stamped `Source=lever_default`; used for jev, which has no
free text, and for claude and openai when the ask is off; `P` is fixed at 0.6). It scores the lever map, not the model, and reports keep the two
apart. Every lever default is scored by lift against the control:

| Intervention | Prediction |
|---|---|
| `grant_shield`, `toughness_boost` (with a target) | `survives` 15 |
| `adjust_player_sensitivity` with a value < 1 (easing), with a target | `survives` 15 |
| `set_player_handicap` with a value > 1 (help; `rein_in` < 1 gets none), with a target | `survives` 15 |
| `adjust_music_tempo` (either direction) | none: the tempo effect on eliminations is confounded with the threshold change, and `activity` (intensity / threshold) moves with the threshold too; see #2487 |
| `send_controller_effect` | `activity_rises` target 10 |
| `adjust_global_difficulty` < 1; `adjust_global_sensitivity` > 2; `set_pacing_profile` `calm` | `no_elimination` 15 (sensitivity is read against the middle level 2, the context has no current level) |
| `adjust_global_difficulty` > 1; `adjust_global_sensitivity` < 2; `set_pacing_profile` `frantic` | `elimination` 30 |
| `play_audio_cue`, `revive_player` (its target is not alive at registration), `eliminate_player`, `end_game`, `noop` | none |

**Model-stated** predictions (`Source=model`) exist only with the ask (§3) and are opt-in.

### 14.3 Scoring

A process-wide prediction book registers each prediction with the values its template needs at time t (the before-value, the
elimination count, the target's alive state, the decide span context). Each prediction gets one timer (t + horizon + 3 s)
through the same timer seam the intervention effect sampler uses; on firing it re-reads the game's current context and scores,
and a prediction whose game partition is gone stays pending for game end. At game end the book scores that game's pending
predictions from the pre-reset snapshot **before** the experiment, retro and summary consumers run (second in the game-end
chain, after the stale counter): early-decidable ones become hit or miss, the rest unscorable, everything `stale_end` on a
stale end. A prediction resolves exactly once (a timer racing game end is claimed under one lock). Bounds: at most 32 pending
per game and 256 overall (the oldest is evicted as unscorable `overflow`); an identical pending prediction is `merged`.
Pending predictions are in memory only and are lost on a restart; the loss shows as registered minus scored counts and a
decide span with no outcome span. A late game end for an unknown game is a no-op.

**Controls (base rates).** Per real or shadow game, every 15 s of play, one `Source=control` prediction (P 0.5, `decide.backend` `none`), cycling through the six templates in turn from a per-game offset (a hash of the game id, so a template's base rate is not always sampled at the same game times) (target templates on a deterministic hash-chosen alive player), at most 12 per game. Control horizons: `survives` 15, `no_elimination` 15, `elimination` 30, `activity_rises` 10, `closeness_rises` 15, `game_ends` 60. They are scored by the same code and give each
template's base rate; **lift = hit rate minus base rate**. A reported hit rate always sits next to its base rate, its unscorable
rate and `game_kind`; the headline uses `acted=true` and real games, and shadow results are labelled "shadow calibration".

**Calibration.** Brier score (p - y)^2 over scored (hit or miss) predictions, as a counter sum, so the rolling value is a
PromQL ratio and no in-process rolling state exists ([telemetry.md §6](telemetry.md#6-metrics)). Per-experiment arm tallies are
report-only: fed in memory, never journaled, never read by the verdict (UC-043 A21, BR-010).

### 14.4 Goal profiles

A **goal profile** is a human-chosen set of up to 4 terms over the same characteristics. Each term: a characteristic, a band
(`min`, `max`, either may be open), a phase (`whole` or `endgame`, the latter meaning at most 2 alive) and a weight.

| Profile | Terms |
|---|---|
| `long_running_game` | `duration` >= 180 s; `activity` 0.3..0.6 (whole game); `elimination_pace` <= 1 per minute |
| `lively_finale` | `activity` >= 0.6 in phase `endgame`; `duration` 90..240 s |
| `balanced_contest` | `closeness` <= 0.25; `duration` >= 120 s |
| `quick_game` | `duration` <= 120 s; `elimination_pace` >= 2 per minute |
| `from_objectives` (default) | derived from the active `objectives` variant, below |

`from_objectives` maps `endurance_focused` to `long_running_game`, `accelerate_focused` to `quick_game`, `balanced_focused`
to `balanced_contest`, `chaos_focused` to `activity` >= 0.6 plus `change` >= 0.3, and `mixed` to none. Every term has weight 1. This is a **mapping, not a
migration**: the rules engine's objective weights and the experiment fitness functions are unchanged, and the `duration` bands
read the same flags as the fitness thresholds (`fitness.endurance.min_session_seconds`,
`fitness.accelerate.target_session_seconds`) so the two cannot drift. `lively_finale` is the form the deferred `finale`
objective of #2467 takes: no new objective or fitness function. The `goal.profile` flag ([flags.md §3](flags.md#3-agent)) parser accepts only the six
characteristic keys, the two phases, finite bands inside per-characteristic hard bounds and at most 4 terms; anything
invalid falls back to `from_objectives` (a WARN once and a metric). It is read per game, so a game's targeting context
(`game_kind`, `experiment_id`, `arm`) can give each arm its own profile.

**Hard bounds.** A band edge must lie inside the characteristic's bound, which is also what makes the schema testable:

| Characteristic | Lower | Upper | Unit |
|---|---|---|---|
| `duration` | 0 | 3600 | seconds |
| `activity` | 0 | 2 | fraction of the death threshold |
| `change` | 0 | 2 | fraction of the death threshold |
| `elimination_pace` | 0 | 30 | eliminations per minute |
| `closeness` | 0 | 2 | fraction of the death threshold |
| `survival` | 0 | 1 | 0 or 1 (the target survived) |

The decisive-end idea ("the game ends soon after two players remain") is not a characteristic; it stays a prediction template (`game_ends`) and is not a goal term. A new characteristic needs its own row in §14.1 first.

**Attainment.** At game end each term is scored by the same `Measure` (in band is 1, otherwise distance-decayed to 0) and the
weighted mean is the game's goal attainment, recorded in the game summary and its span (UC-042 A12, BR-009).

**Goals reach the model as structured targets, never as prose.** Only when `decision.goal_in_prompt` is on or
`decision.prompt_profile` (implemented in #2467 PR 5 with the neutral lever lines only, no goal lines yet) is `neutral`, `Render` adds two lines to the user message, for example
`goal: long_running_game duration>=180s activity=0.30..0.60 elimination_pace<=1/min` and
`now: duration=95s activity=0.42 elimination_pace=0.6/min closeness=0.21`, and the neutral system prompt drops the
"closer, more dramatic" sentence in favour of "pursue the goal targets in the state". Under v1 both flags are off: no line,
and system and user bytes are unchanged. jev gets a goal criterion only under the neutral profile, in a later slice.

### 14.5 Safety and defaults

| Flag | Default | Unreadable value |
|---|---|---|
| `decision.predictions_enabled` | on (rules, lever and control predictions cost no call and no prompt bytes; read per decision cycle; off stops new registrations and controls, predictions already pending still score and emit) | on (it only observes) |
| `decision.predictions.ask_model` | off (it changes the request bytes) | off |
| `goal.profile` | `from_objectives` | `from_objectives` |
| `decision.goal_in_prompt` | off | off |

All four are `agent`-set flags, human-owned, read only by the agent; the agent never writes them (C-034; covered by the no-writes-to-the-`agent`-set invariant of the never-written test, which holds no key list). A test pins that `runDecision` outcomes are identical with predictions on and off, and
that the rules-path golden is unchanged.
