# Post-game retrospective capture (#844)

When a game ends, the agent has the richest data it will ever have — the full
elimination sequence, the final duration, the per-player skill spread — and the
suggestions it could make (calibration tweaks) take effect at the *next* game's
init. This issue adds a **post-game analyst** path: on the `GameActive`
true→false transition the agent builds a retrospective prompt from the full
session and would ask an LLM to suggest **calibration tweaks for the next game**.

This is the LLM use case that tolerates cloud latency: in-game decisions need
fast local inference, but between games even a slow backend (or `claude -p` under
a Max subscription) is fine.

## Capture-first (same pattern as #739)

No backend is called yet. Exactly like the #739 in-game spike, this path
**captures** the prompt it would send onto a dedicated `agent.llm.retro` span and
falls through; a follow-up wires a real backend once the fallback chain (#741)
exists. The prompt is replayable offline via `services/agent/scripts/replay-prompt.sh`.

## Trigger design

The trigger is the `gamecontext.Store` `OnGameEnd` hook (`gamecontext/store.go`),
fired exactly once on the `GameActive` true→false transition inside
`SetGameActive`. The hook receives a **pre-reset** snapshot — the `SessionID` and
the `EliminationSequence` are still intact (the snapshot is taken before any
session-grace reset). The callback is invoked **after** the store mutex is
released, never under `s.mu`, so it may read the store / build telemetry without a
re-entrancy deadlock. `OnGameEnd == nil` disables the hook (behavior-neutral).

`decision.RetroCoordinator.OnGameEnd` is the consumer. It is defensive:

- skips if the snapshot still reports `GameActive` (only end-of-game captures);
- skips if `SessionID` is empty;
- skips if `SessionID` equals the last captured session (exactly-once dedupe).

### No interaction with the intervention budget

The coordinator **never** touches `gate.ShouldEvaluate`, the decision `Loop`, or
the rate limiter — there is simply no code path from a retrospective to the
limiter. A retrospective therefore cannot consume the in-game intervention
budget. This is a structural guarantee (no shared state), not just a convention.
Flags are evaluated with a `context.Background()` (there is no inbound RPC context
at game end), so the retro span is a standalone root — intentional, it is not a
child of any OTLP Export.

## Span / attribute schema

Span name: **`agent.llm.retro`** (constant `decision.SpanLLMRetro`). Built by the
single attribute builder `decision.retroPromptAttributes` (one producer,
schema-complete on every emission, mirroring `llmPromptAttributes`). Every
attribute is present on every emission:

| Attribute | Value | Source |
|-----------|-------|--------|
| `gen_ai.operation.name` | `"chat"` | semconv `GenAIOperationNameChat` |
| `gen_ai.request.model` | the `model` capability flag | semconv `GenAIRequestModel` |
| `gen_ai.output.type` | `"json"` | semconv `GenAIOutputTypeKey` (no JSON constant in semconv v1.34.0) |
| `agent.mode` | `"retro"` | distinct from the in-game `"llm"` |
| `agent.objectives` | sorted `k=v` weights | `summarizeObjectives` (shared) |
| `interventions.allowed` | allow-list summary | `allowedSummary` (shared) |
| `session.id` | the finished session's id | |
| `llm.retro.system` | full System prompt text (uncapped) | `RetroPrompt.System` |
| `llm.retro.user` | full User prompt text (uncapped) | `RetroPrompt.User` |
| `llm.retro.bytes` | `len(system)+len(user)` (int) | |
| `inference.configured` | the `model` capability flag | |
| `inference.used` | **`"none"`** | see divergence below |
| `inference.fallback_reason` | `"no_backend_available"` | `decision.FallbackNoBackend` |

The companion log line `agent.llm.retro_captured` carries only metadata
(`session_id`, `model`, `bytes`, `fallback_reason`) — never the prompt text,
which lives on the span alone.

### Attribution divergence: `inference.used = "none"`, not `"rules"`

The in-game capture (#739) records `inference.used = "rules"` because the in-game
path falls back to the deterministic rules engine — "rules" is the honest answer
to "what decided this cycle". A retrospective has **no rules fallback**: nothing
runs in place of the offline analyst at game end. Claiming `"rules"` would be a
lie. `inference.used = "none"` (`decision.DefaultInference`) is the honest value —
no inference of any kind ran. #741's backend will supply `used="llm"` once it
answers.

## JSON response contract → calibration surface (#766)

The System prompt (rendered in `services/agent/llm/retro.go`) asks the model for
**exactly one JSON object**, no prose:

```json
{
  "session_assessment": "<one short sentence: how did this game go?>",
  "suggestions": [
    {
      "flag": "<one of: global_difficulty_factor, pacing_profile, threshold_table, objective_variant>",
      "value": "<the suggested value as a string>",
      "reason": "<one short sentence tying the change to session evidence>"
    }
  ]
}
```

The `flag` field is constrained to the **calibration surface** — the #766 flags
read at game **init** (docs/research/722-intervention-surface.md §11), NOT the
in-game intervention allow-list:

| Suggestion `flag` | Calibration flag (read at init) |
|-------------------|---------------------------------|
| `global_difficulty_factor` | continuous global movement-demand scale (~0.5..1.5) |
| `pacing_profile` | music-schedule preset (`relaxed`/`standard`/`intense`) |
| `threshold_table` | named death/warning threshold table (`easy`/`standard`/`hard`) |
| `objective_variant` | next-game session goal weighting (endurance/balanced/accelerate/chaos) |

The policy is "suggest the smallest change that addresses an observed problem; if
the session looked healthy, return an empty `suggestions` list." Suggestions are
**recorded only**, never auto-applied in this issue.

## Determinism

`llm.BuildRetro` is pure and deterministic, the same contract as `llm.Build`: the
injected `Now`, players sorted by serial, no wall clock, fixed float precision
(2 decimals). The same `RetroInput` always yields a byte-identical `RetroPrompt`.
Golden tests (`llm/testdata/retro_*.golden`, regenerated with `-update`) and a
`TestRetroDeterminism` byte-identity test lock this down. `TestPlayerOutcome` and
`TestWinnerSerial` unit-test the outcome derivation:

- per-player outcome comes from the elimination-sequence position
  (`eliminated #1` = first out); a serial absent from the sequence is a `survivor`;
- `winner` is the sole survivor's serial when exactly one player survived, else
  `unknown` (zero or multiple survivors are both ambiguous).

## Exactly-once design

Two layers guarantee one retro per session:

1. The store fires `OnGameEnd` only on the true→false edge (not on start, not on
   a repeated `false`).
2. The coordinator dedupes on `SessionID` (a mutex-guarded `lastSession`): the
   same id twice emits one span; a new id emits a second.

## The minimal store-hook constraint (#845)

`gamecontext/store.go` will be rewritten by #845 (lifecycle handling). The hook
added here is deliberately **minimal and additive**: one `OnGameEnd` field, a
`snapshotLocked()` helper factored out of `Snapshot()`, and a few lines in the
existing true→false branch. The signature of `SetGameActive` is unchanged. A code
comment on `OnGameEnd` records this constraint so #845 keeps the surface small.

## Manual replay

`scripts/replay-prompt.sh` replays an `agent.llm.retro` span **identically** to an
`agent.llm.prompt` span — copy the `llm.retro.system` / `llm.retro.user`
attribute values out of Jaeger into two files and run the script. (The script
only invokes the `claude` CLI headless; it does not validate the JSON — the spike
is a manual eyeball.)

## Forward pointers

- **#741 `resolve_backend()`** — wires a real inference backend; supplies the
  honest `inference.used = "llm"` and unmarshals the reply.
- **`FlagConfigWriter` auto-apply (follow-up)** — applying a suggested tweak to
  the calibration flags behind a flag, with human review, is separate work.
- **#847 intervention budget** — once backends exist, a retro counts as **one LLM
  call** for budgeting purposes; it still does not draw from the in-game
  per-minute intervention budget.
