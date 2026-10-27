# Service tier (T2)

Real services from source at HEAD, wired over their real gRPC and flag boundaries, on a fresh flag plane.
No images, no compose. Tier model and the TC-to-tier map: the latest plan comment on #1422.

```bash
make test-service                 # all of tests/service (needs uv, go, docker)
make test-service TEST=tc018      # -k filter
```

CI: the `service-tests` job (path-filtered on PRs, always on main, part of `ci-complete`).

## What a stack is

| Part | Real or fake | How |
|---|---|---|
| flag service | real, `go build` of `services/flag-service` | subprocess; binary cached by source hash in `~/.cache/joustmania-service-tier` (`JOUSTMANIA_SERVICE_CACHE`); temp state file; baseline = temp copy of `services/flagd/ci` |
| flagd | real, compose image tag | container; `--uri grpc://host.docker.internal:<flag-service port>` (its only source, as in the stack); RPC 8013 and sync 8015 on fixed ephemeral host ports |
| controller-manager, game-coordinator, menu | real, from source | `python services/<svc>/server.py` with the service-tests venv; `GRPC_PORT` / `MOCK_CONTROL_PORT` on ephemeral ports; ready on gRPC health SERVING |
| audio | fake | `FakeAudio`, in-process gRPC server that records every call (`stack.fake_audio.calls_of("PlaySound")`) |
| otel-collector, Jaeger | none by default; `OtlpSink` fake on opt-in | default: `OTEL_SDK_DISABLED=true` and `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:1` in every Python service. `Composition(..., otlp=True)`: SDK on, endpoint = the in-process `OtlpSink` (`stack.otlp_sink`, compose name `otel-collector:4317`). The flag service gets no endpoint either way (no-op) |
| agent | real, `go build` of `services/agent` (`AGENT` composition only) | subprocess; binary cached by source hash next to the flag service's; OTLP receiver on an ephemeral port (compose `agent:4317`), flagd RPC port, the stack's flag service; `OTEL_BSP_SCHEDULE_DELAY=200`, `AGENT_LLM_MAX_REQUESTS_PER_MINUTE=600`, dummy `DECISION_OPENAI_API_KEY` |
| OpenAI-compatible backend | fake (`AGENT` only) | `tests/integration/fake_openai/server.py` as a subprocess (`stack.fake_openai`: `script()`, `release()`, `requests(game_id)`); the stack rewrites the `fake` variant of `decision.openai.base_url` in its temp baseline copy to it |
| Jev (TypeSafe System One) | fake (`AGENT_JEV` only) | `harness/fakes/jev.py`, in-process threaded server (`stack.fake_jev`: `script(mode, intervention=, confidence=)`, `requests(stage)`); the stack rewrites the `ci/` `fake` variant of `decision.jev.base_url` to it and gives the agent the fake's dummy `JEV_API_KEY` (other compositions have none) |
| Claude (Anthropic Messages API) | fake (`AGENT_CLAUDE`: text backend; `AGENT_CLAUDE_DECIDE`: in-game decide backend, #1900) | `harness/fakes/claude.py`, in-process threaded server (`stack.fake_claude`: `script(retro=, proposal=, decide_mode=, intervention=, confidence=)`, `requests(kind)` with kind `retro`/`proposal`/`decide`); the stack writes the `inference.backend=claude` overlay (the flag wins over the env, #1810) before the agent starts, and the agent gets `ANTHROPIC_API_KEY` and `AGENT_INFERENCE_BASE_URL` = the fake, `AGENT_EXPERIMENT_DIR` in the workdir |
| GitHub (REST API, issues and PRs) | fake (`Composition(fake_github=True)`, needs `agent`) | `harness/fakes/github.py`, in-process threaded server (`stack.fake_github`: `requests(kind)`, `html_url(kind, n)`); the agent gets the promoter env gate (`AGENT_CODE_IMPROVEMENT_ENABLED`, target `github`, a dummy `GITHUB_TOKEN` and `GITHUB_REPOSITORY`) with `GITHUB_API_URL` = the fake, so a promotion never reaches github.com (TC-011 promotion) |
| rust-hid, python-hid, BlueZ | none | controller manager: `RUST_HID_*` / `PYTHON_HID_*` = `127.0.0.1:1`, dead `DBUS_SYSTEM_BUS_ADDRESS` |

Python services run as subprocesses, not in-process: OpenFeature domains, the rate limiter and the OTel
providers are module-global.

**Fidelity: telemetry is opt-in.** By default (`OTEL_SDK_DISABLED`) every span is non-recording (its
context is invalid, so the `is_valid` branches never run, e.g. the span ids behind trace correlation) and
the OTel metrics and logs are no-ops. A module that asserts telemetry opts in with
`SERVICE_COMPOSITION = dataclasses.replace(GAME, otlp=True)`: the services export over their real OTLP/gRPC
exporters to `stack.otlp_sink` (`harness/fakes/otlp.py`), which records spans and metrics:

```python
span = await stack.otlp_sink.wait_span_async(None, {"game.id": game_id}, event="player_disconnected")
root = await stack.otlp_sink.wait_span_async("Joust Free-for-All", {"game.id": game_id}, root=True)
point = await stack.otlp_sink.wait_metric_async("game_trace_correlation", {"game_id": game_id})
```

- A span arrives when it ENDS, after the BatchSpanProcessor delay (`otel_bsp_schedule_delay_millis`,
  5 s default); metrics every `metrics_export_interval_ms`. Use timeouts of ~30 s.
- `wait_span` returns the oldest matching span, `wait_metric` the latest matching point (cumulative).
  The sink lives as long as the module-scoped stack and is not cleared between tests: always filter by
  `game.id` / `game_id`, or an earlier test's record matches at once. Logs are accepted and counted only.
- `OtlpSink(forward_to="host:port")` tees every trace and metrics export to another OTLP/gRPC receiver
  (the agent's, like the collector's `otlp/agent` exporter); failures count in `forward_errors`.
- Still full stack (T3): the collector's processing (filters, `span_metrics`, transforms), VictoriaMetrics
  names and PromQL, Jaeger queries.

Proof: `test_otlp_sink.py` (a game's root span and `player_disconnected` reach the sink).

Compositions (`harness/stack.py`): `GAME` = controller-manager + game-coordinator, `LOBBY` = `GAME` + menu,
`AGENT` = `LOBBY` with `otlp=True` plus the agent and the fake OpenAI backend (#1600). In `AGENT` the sink
tees the services' traces and metrics to the agent (the collector's role) and the agent exports its own
spans and metrics back to the sink (Jaeger's role): read `agent.decide` & co. with
`stack.otlp_sink.spans("agent.decide", {"game.id": game_id}, service="agent")`. Shared journey steps
(agent flags, menu-started FFA game, decide-span helpers): `agent_flow.py`. Agent journeys run on
`fresh_stack`: `enabled`, `interventions_allowed` and the agent's LLM budget are global.

Connection faults: `TcpProxy` (`harness/fakes/tcp_proxy.py`) forwards one service's dial to another and
`drop_connections()` cuts it without a restart. A module fixture creates it, sets the module's
`SERVICE_COMPOSITION` with the client pointed at it through `Composition.env` (e.g. the menu's
`CONTROLLER_MANAGER_PORT`), requests `service_stack`, calls `start(<target port>)` and owns `stop()` in a
`finally`; never create one at import. `test_uc002_button_stream_reconnect.py` (UC-002 A5) is the example.

## Writing a test

```python
import pytest
from tests.integration.helpers import get_game_client          # reused unchanged
from tests.service.harness import LOBBY

pytestmark = pytest.mark.tier("service")   # required: collection fails without it
SERVICE_COMPOSITION = LOBBY                # optional, default GAME

@pytest.mark.uc("UC-053", flow="main", tc="TC-0xx")
async def test_x(service_stack, flag_service):
    game_client, channel = await get_game_client(service_stack)
    ...
```

- `service_stack` (module scope) is shared by the module's tests; `fresh_stack` (function scope) gives a
  test its own stack, for process-global state (stack-wide flags, the rate limiter, restarts).
- The stack answers `get_service_host(name, port)` / `get_service_port(name, port)` with compose names and
  container ports (`flagd` 8013/8015, `flag-service` 8070, `controller-manager` 50052/50062,
  `game-coordinator` 50053, `menu` 50054, `audio` 50056), so `tests/integration/helpers.py` and
  `tests/integration/flag_service.py` take it in place of `docker_compose`. A service not in the
  composition raises `KeyError`.
- `flag_service` binds the integration FlagService client to the test's stack and deletes stack-wide
  overlays at teardown; `headless_cleanup` force-ends registered games and removes reserved controllers.
- Restarts: `stack.flag_plane.restart_flag_service()`, `stack.flag_plane.restart_flagd()`,
  `stack.services["game-coordinator"].restart()`; each waits for readiness, same ports. They block (up to
  45 s), so an async test may call them through `asyncio.to_thread`: every spawn runs on one long-lived
  spawner thread, because `PR_SET_PDEATHSIG` fires when the *forking thread* exits, not the process.
- Wait on observable conditions (`poll_until`, `async_poll_until`, health), never a sleep. A fixed window
  is allowed only as an absence assertion, with a comment saying so.
- A failed test gets every process's log tail attached to its report. Logs live under the stack's
  pytest tmp dir (`logs/<service>.log`).

## Teardown guarantees

- Linux: every subprocess runs in its own process group with `PR_SET_PDEATHSIG` (forked from the spawner
  thread, which lives as long as pytest): it dies with pytest, even on `kill -9`.
- macOS (and musl): there is no `PR_SET_PDEATHSIG`. A normal exit, Ctrl-C and SIGTERM stop every process
  (fixture teardown and the `atexit` sweep); a `kill -9` of pytest leaks the service processes, which you
  then stop by hand (`pkill -f 'services/.*/server.py'`, `pkill -f flag-service-`).
- SIGTERM (CI cancel, `timeout`) is turned into `KeyboardInterrupt` (`conftest.py`), so fixtures and the
  `atexit` sweeps run on every OS. The `atexit` sweep also removes this run's flagd containers.
- flagd containers carry the `joustmania.service` ownership labels (`tests/contract/flagd_harness.py`
  `Ownership`). A start removes containers whose owner pid is gone (same host) or that are older than
  30 min; a live parallel run's container (other worktree) is left alone.
- The controller manager gets a dead `DBUS_SYSTEM_BUS_ADDRESS`, so it never touches the host's BlueZ, and
  unreachable rust-hid / python-hid addresses, so it never drives real controllers even when a compose
  stack on the same host publishes rust-hid on `127.0.0.1:50058` (`ServiceStack.service_envs`,
  `test_harness.py`).
- Port clashes fail loudly: the Python services bind without `SO_REUSEPORT` (`lib.grpc_utils.get_server_options`),
  and flagd's host ports are picked right before `docker run`.

## Extending (S2+)

- New Python service: add it to `SERVICES` in `harness/stack.py` (source dir, compose port, OTel name) and
  its env wiring in `_start`. The service's `__main__` must read `GRPC_PORT` (`lib.grpc_utils.port_from_env`).
- New fake (fake Claude, fake OpenAI): an in-process server with `start() -> port` / `stop()` like
  `FakeAudio` or `OtlpSink` (`harness/fakes/`), started in `ServiceStack._start` and published under its
  compose name in `_ports`; point the services at it through `Composition.env` or the common env (as
  `Composition.otlp` does for the OtlpSink).
- Agent composition: done (`AGENT`, `harness/agent.py`, `harness/fakes/openai.py`, #1600).
- A moved TC keeps its `tc="TC-XXX"` tags and gets a `**Tier:**` line in its Automation section.

`Composition(..., agent_env={...})` adds env to the agent process (TC-011 seeds its experiment this way);
the agent's experiment journal always goes to `<stack workdir>/experiments`.
