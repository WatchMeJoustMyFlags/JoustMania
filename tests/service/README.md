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
| otel-collector, Jaeger | none | `OTEL_SDK_DISABLED=true` in every Python service |

Python services run as subprocesses, not in-process: OpenFeature domains, the rate limiter and the OTel
providers are module-global.

Compositions (`harness/stack.py`): `GAME` = controller-manager + game-coordinator, `LOBBY` = `GAME` + menu.

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
  `stack.services["game-coordinator"].restart()`; each waits for readiness, same ports.
- Wait on observable conditions (`poll_until`, `async_poll_until`, health), never a sleep. A fixed window
  is allowed only as an absence assertion, with a comment saying so.
- A failed test gets every process's log tail attached to its report. Logs live under the stack's
  pytest tmp dir (`logs/<service>.log`).

## Teardown guarantees

- Every subprocess runs in its own process group with `PR_SET_PDEATHSIG`: it dies with pytest, even on
  `kill -9`. A normal exit also runs an `atexit` sweep.
- flagd containers carry the `joustmania.service` ownership labels (`tests/contract/flagd_harness.py`
  `Ownership`). A start removes containers whose owner pid is gone (same host) or that are older than
  30 min; a live parallel run's container (other worktree) is left alone.
- The controller manager gets a dead `DBUS_SYSTEM_BUS_ADDRESS`, so it never touches the host's BlueZ.

## Extending (S2+)

- New Python service: add it to `SERVICES` in `harness/stack.py` (source dir, compose port, OTel name) and
  its env wiring in `_start`. The service's `__main__` must read `GRPC_PORT` (`lib.grpc_utils.port_from_env`).
- New fake (OtlpSink, fake Claude, fake OpenAI): an in-process server with `start() -> port` / `stop()`
  like `FakeAudio`, started in `ServiceStack._start` and published under its compose name in `_ports`;
  point the services at it through `Composition.env` or the common env (for OTLP: drop
  `OTEL_SDK_DISABLED` and set `OTEL_EXPORTER_OTLP_ENDPOINT`).
- Agent composition: the agent is a Go binary like the flag service; reuse `ManagedProcess` with a
  `go build` cached by source hash (see `flag_plane.build_flag_service`).
- A moved TC keeps its `tc="TC-XXX"` tags and gets a `**Tier:**` line in its Automation section.
