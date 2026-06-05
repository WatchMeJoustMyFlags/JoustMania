# M4 prompt-capture spike (#739)

The agent does not call an LLM yet. This spike makes the agent build — and record
on telemetry — the exact prompt it *would* send to a backend on every `llm`-mode
decision cycle, so we can replay those prompts against a real model offline and
judge whether the response contract is workable before committing to a backend
(#741) or to an auth story (#742).

This is delivered in two stacked PRs:

- **PR 1 (#841)** — `services/agent/llm`: a pure, deterministic prompt builder,
  `llm.Build(llm.BuildInput) llm.Prompt`. Nothing imported it.
- **PR 2 (this PR)** — wires prompt capture into the decision loop and emits it
  on a dedicated span, then falls back to the rules engine exactly as before.

## What the spike does

When `agent.mode = "llm"`, the decision loop (`decision/decision.go`):

1. builds the prompt for the cycle via `llm.Build` (using the loop's injected
   clock, never the wall clock, so the prompt is deterministic);
2. records the full prompt on a dedicated `agent.llm.prompt` span;
3. falls back to the deterministic rules engine — the rules decision is what
   actually drives any intervention, identical to `mode = "rules"`.

No model is called. `inference.used` is `"rules"` and
`inference.fallback_reason` is `"no_backend_available"` on every llm cycle until
#741 wires a backend.

### Why a dedicated span

The `agent.decision` audit spans are **lazy**: they are emitted only on cycles
where the rules engine returns at least one decision (the trace is the audit log
of agent *activity*, not of idle time — see `decision/decision.go`,
`OnEvaluate`). Most llm-mode cycles are idle, so attaching the prompt to the
decision span would lose it exactly when we most want to inspect it. A dedicated
`agent.llm.prompt` span emits on every (throttled) llm cycle regardless of
whether a decision was produced, and is greppable in Jaeger by name.

### Throttle

The capture span and its `agent.llm.prompt_captured` log line share the single
per-cycle throttle decision with the `agent.evaluate` log and the
`agent.disabled` span (`decision.throttle_seconds`, default 1s). A steady-state
llm agent under heavy signal load emits at most one capture per interval, and the
prompt is built only when it will actually be emitted (no serialization cost on
throttled-out cycles).

## Span / attribute schema

Span name: **`agent.llm.prompt`** (constant `decision.SpanLLMPrompt`). Built by
the single attribute builder `decision.llmPromptAttributes` (one producer,
schema-complete on every emission, mirroring the `infra_telemetry.go`
convention). Every attribute is present on every emission:

| Attribute | Value | Source |
|-----------|-------|--------|
| `gen_ai.operation.name` | `"chat"` | semconv `GenAIOperationNameChat` |
| `gen_ai.request.model` | the `model` capability flag | semconv `GenAIRequestModel` |
| `gen_ai.output.type` | `"json"` | semconv `GenAIOutputTypeKey` (no JSON constant in semconv v1.34.0; key + literal) |
| `agent.mode` | `"llm"` | |
| `agent.prompt_variant` | resolved variant (`llm.Prompt.Variant`) | |
| `agent.objectives` | sorted `k=v` weights | `summarizeObjectives` (shared with decision span) |
| `interventions.allowed` | allow-list summary | `allowedSummary` (shared with decision span) |
| `llm.prompt.system` | full System prompt text (uncapped) | `llm.Prompt.System` |
| `llm.prompt.user` | full User prompt text (uncapped) | `llm.Prompt.User` |
| `llm.prompt.bytes` | `len(system)+len(user)` (int) | |
| `inference.configured` | the `model` capability flag | |
| `inference.used` | `"rules"` | rules engine actually decided |
| `inference.fallback_reason` | `"no_backend_available"` | `decision.FallbackNoBackend` |

The full prompt text is uncapped: the Go SDK applies no attribute length limit
and the collector does not truncate, so the entire prompt survives to Jaeger for
replay. The `agent.llm.prompt_captured` log line carries only metadata
(`session_id`, `variant`, `model`, `bytes`, `fallback_reason`) — never the prompt
text, which lives on the span alone.

### `fallback_reason` rename

The pre-spike placeholder `inference.fallback_reason = "llm_path_not_implemented"`
(`FallbackLLMNotImplemented`) is replaced by `"no_backend_available"`
(`decision.FallbackNoBackend`). The prompt path now exists — what is missing is a
*backend*. #741's `resolve_backend()` will supply the real reason (and
`inference.used = "llm"`) once a backend answers.

## JSON response contract → `Decision`

The System prompt's RESPONSE CONTRACT (rendered in `services/agent/llm/prompt.go`)
asks the model for **exactly one JSON object**, no prose:

```json
{
  "intervention": "<one of interventions.allowed, or \"noop\">",
  "target_serial": "<player serial, or \"\" for session-scoped>",
  "value": "<intervention payload as a string, or \"\" for the default>",
  "reason": "<one short sentence explaining the choice>",
  "objective_served": "<one of: endurance, balanced, accelerate, chaos>"
}
```

These map field-for-field onto `decision.Decision`:

| JSON field | `Decision` field |
|------------|------------------|
| `intervention` | `Intervention` |
| `target_serial` | `TargetSerial` |
| `value` | `Value` |
| `reason` | `Reason` |
| `objective_served` | `ObjectiveServed` |

`noop` plus a reason is the "no intervention warranted" reply. Once #741 lands,
the backend's reply unmarshals straight into a `Decision` that flows through the
existing permission chain (allow-list, battery threshold, rate limit) — a
model-chosen intervention outside `interventions.allowed` is blocked, never
silently applied.

## Manual replay

1. Bring up the observability stack and run the agent in llm mode
   (`agent.mode = "llm"`) so it emits captures.
2. Open Jaeger (`http://localhost:8080/jaeger/`) and find an `agent.llm.prompt`
   span.
3. Copy the `llm.prompt.system` attribute value into `system.txt` and
   `llm.prompt.user` into `user.txt`.
4. Run the replay helper and eyeball the response:

   ```bash
   services/agent/scripts/replay-prompt.sh system.txt user.txt | jq .
   ```

   Confirm the reply is a single JSON object with the contract fields. The script
   only invokes the `claude` CLI headless (`-p --append-system-prompt`); it does
   not validate the JSON — the spike is a manual eyeball.

## Forward pointers

- **#741 `resolve_backend()`** — wires a real inference backend; supplies the
  honest `inference.used = "llm"` / `inference.fallback_reason` and unmarshals
  the reply into a `Decision`.
- **#742 Claude auth (undecided)** — we have a Claude Max subscription and no API
  key, so the `claude` CLI in headless mode is the current candidate path. Not
  yet settled.
