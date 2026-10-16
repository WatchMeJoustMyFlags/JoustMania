# Agent inference: who calls which model

The agent talks to models **directly**. There is no gateway or router in
between (see [NORTHSTAR](NORTHSTAR.md): hosted inference is allowed, local
inference is not a goal, and the rules engine is the offline floor). The LiteLLM
gateway (#1049) and the in-game `mode` / `model` / resolver path were removed in
#1234.

There are two separate seams:

| Seam | Used for | Selected by | Backends |
|------|----------|-------------|----------|
| **Decision backend** | in-game decisions (FFA) | `decision.backend` flag in `agent.json`, live | `rules` (default, floor), `claude`, `openai`, `jev` |
| **Text backend** | post-game retro, shadow-experiment proposer | `AGENT_INFERENCE_BACKEND` env, at startup | `stub` (default), `claude` |

## In-game: the decision backend (epic #1225)

`decision.backend` picks who answers the typed decide contract
(`services/agent/decide`). Flip it with `./scripts/agent-backend.sh`; flagd
hot-reloads it, no restart. If the selected backend errors, times out, returns
an invalid answer, or the game is not eligible, the rules engine decides and the
`agent.decide` span records `decide.fallback_reason`.

| Flag | Meaning |
|------|---------|
| `decision.backend` | `rules` / `claude` / `openai` / `jev` |
| `decision.act_confidence_threshold` | below it a remote answer is held, not acted on |
| `decision.min_interval_ms` | per-game cadence of remote calls |
| `decision.claude.model`, `decision.claude.timeout_ms` | Claude backend |
| `decision.openai.base_url`, `.model`, `.timeout_ms`, `.reasoning_effort` | any hosted OpenAI-compatible endpoint |
| `decision.jev.base_url`, `.model`, `.timeout_ms` | Jev, TypeSafe's System One API (`latest` = `jev-latest`, `pinned` = `jev-1.13.0`) |
| `llm.eligible_game_kinds` | game kinds that may call a remote backend (default `real`: shadow games stay on rules) |
| `llm.max_requests_per_minute` | global per-minute cap across all remote decide calls and retros |

Keys come from the agent's environment: `ANTHROPIC_API_KEY`,
`DECISION_OPENAI_API_KEY`, `JEV_API_KEY`. A backend without its key is
unavailable and falls back to rules. `AGENT_LLM_MAX_REQUESTS_PER_MINUTE`
overrides the per-minute cap for a live run.

## Offline: the text backend (#1234)

The post-game retro (`agent.llm.retro`) and the shadow-experiment proposer send a
System + User prompt and read raw text back
(`Infer(ctx, llm.Prompt) (string, error)`). `services/agent/inference/claude.go`
implements that with the official Anthropic Go SDK.

| Env | Default | Meaning |
|-----|---------|---------|
| `AGENT_INFERENCE_BACKEND` | `stub` | `claude` enables the Claude client; anything else is the stub |
| `ANTHROPIC_API_KEY` | (empty) | required for `claude`; without it the agent logs a warning and stays on the stub |
| `AGENT_INFERENCE_MODEL` | `claude-opus-5-5` | model id |
| `AGENT_INFERENCE_EFFORT` | `medium` | `output_config.effort` (not sent to Claude Haiku 4.5, which rejects it) |
| `AGENT_INFERENCE_TIMEOUT_SECONDS` | `120` | per-call deadline, also the retro's deadline |

With the stub, the proposer proposes nothing and the retro is capture-only
(`inference.used=none`, `inference.fallback_reason=no_backend_available`). With
`claude`, a retro calls the model only for game kinds in `llm.eligible_game_kinds`
(default `real`); a shadow-game retro stays capture-only
(`inference.fallback_reason=llm_not_eligible`), so shadow games cannot use up the
budget that real-game decisions need. A retro that calls the model draws one slot
from `llm.max_requests_per_minute` (or `AGENT_LLM_MAX_REQUESTS_PER_MINUTE`).

The proposer runs on every game end, but only while the `enabled` kill switch and
`experiments_enabled` are both on (read at each game end; `enabled` fails closed
when flagd is unreachable). It checks again just before writing `game.json`, so
switching the agent off during a model call still blocks the write. With `claude`, a
proposal that passes the cadence floor (`code_improvement.min_interval_seconds`)
calls the model only for game kinds in `llm.eligible_game_kinds` and only when a slot
of the same shared budget is free. Otherwise it records
`agent_llm_gated_total{reason=llm_not_eligible|llm_budget_exhausted}` and retries
at the next game end (#1270). The stub never draws from the budget.

The `code_improvement.engine` flag (`stub` / `claude`) is only recorded on the
propose span; it does not select anything. `AGENT_INFERENCE_BACKEND` does.

On the models that accept it (Opus 5.5, Opus 5, Sonnet 5.5, Fable 5.1) the client
sends `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`), so a policy
refusal is re-run on a fallback model inside the same call.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
AGENT_INFERENCE_BACKEND=claude docker compose --profile agent up -d agent
```

Keep keys out of the tracked `.env`; export them or use the git-ignored
`.env.local` (`docker compose --env-file .env --env-file .env.local up`).

## Telemetry

Every model call is one GenAI-semconv `chat {model}` CLIENT span
(`gen_ai.operation.name`, `gen_ai.provider.name`, request/response model, token
usage, finish reason, `server.address`). It nests under `agent.decide` (in game)
or `agent.llm.retro` (post game), and the client injects W3C `traceparent` on
the request. Failures record only a low-cardinality `error.type` (HTTP status,
`timeout`, `refusal`, ...); vendor error text never reaches a span (#1242).

## History

#1046 designed a LiteLLM gateway in front of an OpenAI-compatible client so the
agent could swap between host Ollama, a Jetson, and the cloud by config. #1225
replaced that with flag-selected backends called directly, and #1234 removed the
gateway, the Ollama/Jetson tiers, the `mode` / `model` / `prompt_variant` flags,
and the in-game `llm.*` gate flags other than `llm.eligible_game_kinds` and
`llm.max_requests_per_minute`. The research notes under `docs/research/` still
describe the older design.
