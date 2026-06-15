# Agent inference gateway — LiteLLM "tool in the middle" (#1049)

The agent talks to **one** OpenAI-compatible endpoint and lets a gateway decide
which provider actually answers. This is the [#1046](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/1046)
recommendation: keep the Go agent clean (one base URL + one key) and move all
provider swap / fallback / key custody into **config**, not Go code.

It builds on [#1048](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/1048):
the agent already has an OpenAI-compatible `Backend.Infer` driven by
`AGENT_INFERENCE_*` env (`services/agent/inference/openai.go`). The gateway just
changes where `AGENT_INFERENCE_BASE_URL` points.

## It is OPT-IN

A plain `docker compose up` runs the stack **without** the gateway. The agent
keeps its default inference backend (`stub`, or Ollama-direct when configured) —
no behavior change. The gateway only exists when you activate the `gateway`
compose profile.

| State | Agent inference | Gateway container |
|-------|-----------------|-------------------|
| **Default** (`up`) | stub / Ollama-direct (`host.docker.internal:11434`) | absent |
| **Gateway** (`--profile gateway` + override) | `openai` → `http://litellm:4000/v1` | running |

## Enable it

```bash
# Ollama-only (free, local, no key):
docker compose \
  -f docker-compose.yml \
  -f docker-compose.gateway.yml \
  --profile gateway up -d

# With the optional Anthropic cloud layer (key lives in the GATEWAY env only):
ANTHROPIC_API_KEY=sk-ant-... docker compose \
  -f docker-compose.yml \
  -f docker-compose.gateway.yml \
  --profile gateway up -d
```

- `docker-compose.yml` defines the `litellm` service behind the `gateway`
  profile (on `:4000`).
- `docker-compose.gateway.yml` flips the **agent's** env to route through it
  (`AGENT_INFERENCE_BACKEND=openai`, `AGENT_INFERENCE_BASE_URL=http://litellm:4000/v1`)
  and makes the agent wait for the gateway to be healthy.

The default stack is untouched — omit the override file and the `--profile
gateway` flag and nothing changes.

## Routing & fallback (`services/litellm/config.yaml`)

The gateway's `model_list` maps **route name → provider**. The route names are a
superset of the `agent.json` `model` flag variants, so a flag flip always
resolves to a real route:

| Route (= agent.json `model`) | Provider | Notes |
|------------------------------|----------|-------|
| `phi4-mini` | host Ollama | **free local default** |
| `gemma3:4b` | host Ollama (or LAN Jetson) | higher-quality local tier (#738) |
| `claude` | Anthropic API | **optional** cloud top layer |
| `copilot` | host Ollama (`phi4-mini`) | dropped in #1046; mapped to local so a stale flag still resolves |

**Fallback chain** (`litellm_settings.fallbacks`): `claude → phi4-mini` and
`gemma3:4b → phi4-mini`. A cloud outage (or an unset key) degrades to the local
model rather than failing inference.

```yaml
litellm_settings:
  fallbacks:
    - claude: ["phi4-mini"]
    - gemma3:4b: ["phi4-mini"]
```

Where the gateway reaches Ollama is `OLLAMA_BASE_URL` (default
`http://host.docker.internal:11434`, the host machine's Ollama via
`extra_hosts: host-gateway`). Repoint it at a LAN Jetson without editing
`config.yaml`.

> Reaching a **host** Ollama is the most common live-inference foot-gun: Ollama
> must bind `0.0.0.0` (not its `127.0.0.1` default), and under **WSL2 with
> Ollama on Windows** the container must dial the WSL default-route gateway IP,
> *not* `host.docker.internal`. See
> [`agent-ollama-host-runbook.md`](agent-ollama-host-runbook.md) for the full
> checklist — both the gateway's `OLLAMA_BASE_URL` and the Ollama-direct
> `AGENT_INFERENCE_BASE_URL` need it.

## The Anthropic key — gateway-only, optional

The Anthropic API key lives **only** in the gateway's env (`ANTHROPIC_API_KEY`
on the `litellm` service). It is **never** mounted into or passed to the agent
container — credentials are not OpenFeature flags. The key is **optional**: with
no key set, the gateway runs Ollama-only and the `claude` route falls back to
local. (Per the [#742](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/742)
spike, use the Anthropic **API key**, NOT Max OAuth — that path is ToS-barred.)

Optionally set `LITELLM_MASTER_KEY` to require a shared key on the proxy's own
API; the agent then sends it via `AGENT_INFERENCE_API_KEY` (wired automatically
by the override from `LITELLM_MASTER_KEY`). Unset = open on the internal docker
network only.

## Control-plane boundary — OpenFeature stays in charge

The gateway sits **below** the agent's control plane; it does not replace it
(#1046):

- **OpenFeature / `agent.json`** (human-owned, `:ro`) owns *selection* — the
  `model`/route, `mode`, `enabled` kill-switch, etc. The agent sends the `model`
  value as the OpenAI `model` param.
- **Gateway config** (infra, NOT OpenFeature) owns route→provider mapping, API
  keys, and fallback chains.

Two invariants the gateway preserves:

1. **Route-name contract** — `agent.json` `model` variants are a subset of the
   gateway routes. An unknown/unreachable route degrades via the fallback chain,
   it does not silently break.
2. **Terminal fallback stays agent-side** — the rules engine "no backend
   answered → rules" last rung lives in the **agent**, so a **gateway outage ≠
   agent outage**. The gateway owns LLM fallback chains; the agent owns the
   final no-cloud-dependency rung.

## Edge / Pi note (not implemented here)

LiteLLM is Python/FastAPI — the **heaviest** gateway option. On the Pi 5 (low
RAM), prefer one of the lighter alternatives from the #1046 survey, keeping the
no-cloud-dependency property:

- **Bifrost** (Maxim AI) — a single static **Go** binary, Apache-2.0, ~50×
  lighter than the Python proxy, with the same OpenAI-compatible surface and
  Ollama + Anthropic fallback. Matches our Go/static-binary stack; preferred if
  the Pi itself must host a router.
- **Ollama-direct** — skip the gateway entirely and point the agent's
  `AGENT_INFERENCE_BASE_URL` straight at Ollama's built-in `/v1/`
  (`http://host.docker.internal:11434/v1`, the #1048 default). Local-only, no
  cloud fallback, lowest footprint.

Either way the agent's rules-engine terminal rung means the edge runs fully
standalone with no cloud dependency.

## Validate / smoke-test

Static (config resolves, gateway is opt-in):

```bash
# gateway profile resolves cleanly (litellm + agent pointed at it):
docker compose -f docker-compose.yml -f docker-compose.gateway.yml \
  --profile gateway config >/dev/null && echo OK

# default stack still excludes the gateway:
docker compose -f docker-compose.yml config | grep -c joustmania-litellm  # -> 0
```

Live (needs the host Ollama running with `phi4-mini` pulled, e.g.
`ollama pull phi4-mini`):

```bash
docker compose -f docker-compose.yml -f docker-compose.gateway.yml \
  --profile gateway up -d litellm
curl -s http://localhost:4000/v1/models
curl -s http://localhost:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"phi4-mini","messages":[{"role":"user","content":"ping"}]}'
```
