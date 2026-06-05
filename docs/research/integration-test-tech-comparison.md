# Research: Integration Test Technology — Python vs Go vs k6

Decision document for the question: *our integration tests are Python because
we started with Python — would switching technology (Go, k6) make setup and
execution faster?* Compares the three options on measured evidence from the
current suite and current upstream documentation, including what a full setup
change would look like for each and what each option's parallelization story
is.

The headline finding: **the wall-clock cost of the suite is almost entirely
language-independent** — Docker image pull/startup and deliberate waiting on
the system under test (game pacing, flagd reload settling, invincibility
windows). A rewrite moves none of it. The one structural speed lever —
running games concurrently against one stack — was unlocked by the
shadow-games work (#774–#779) at the *coordinator* level and is equally
available to every client language. **Recommendation: keep Python**, with
falsifiable triggers for when to revisit (§9).

## 1. Where the time actually goes (measured)

From CI run [27028616936](https://github.com/WatchMeJoustMyFlags/JoustMania/actions/runs/27028616936)
(2026-06-05, `main`, fast path with prebuilt GHCR images):

| Phase | Wall clock | Language-dependent? |
|---|---|---|
| Job overhead (checkout, buildx, GHCR login, uv setup) | ~10 s | no |
| Compose pull + start (13 containers) + venv, inside the session fixture | ~50–60 s | no |
| Test execution, 25 tests, strictly serial | ~270 s | **almost none** (see below) |
| **Total integration job** | **~5 min 40 s** | |

pytest reports `25 passed in 325.55s`; the per-suite split from log timestamps:

| Suite | Tests | Time | Dominated by |
|---|---|---|---|
| `test_concurrent_games.py` | 3 | ~30 s | game pacing on headless games |
| `test_f6_flow.py` | 4 | ~43 s | flagd reload settles + revert verification |
| `test_full_game_lifecycle.py` | 10 modes | ~125 s | game-mode pacing (FFA 5 s … FightClub 29 s) |
| `test_intervention_flow.py` | 8 | ~76 s | `RELOAD_SETTLE_SECONDS = 2.0` sleeps + polling |

On the slow CI path (Dockerfiles/deps changed) image *builds* are added on
top — minutes, and entirely language-independent. Locally, a cold
`make test-integration` builds 13 service images (~15–20 min); that is the
"setup takes too long" feeling, and no test-harness language changes it.

### 1.1 What the harness itself costs

The Python process spends its life **waiting**: on gRPC streams, on polling
loops, on `asyncio.sleep`. Interpreter overhead is microseconds against
multi-second waits. Concretely, the in-test time consists of:

- **System pacing** — invincibility windows (2 s floor in
  [game.ci.json](../../services/flagd/game.ci.json)), FightClub inter-round
  pauses ([test_full_game_lifecycle.py:125](../../tests/integration/test_full_game_lifecycle.py)),
  "let game run briefly" before force-end
  ([test_full_game_lifecycle.py:132](../../tests/integration/test_full_game_lifecycle.py)).
- **flagd settle sleeps** — `RELOAD_SETTLE_SECONDS = 2.0`
  ([test_intervention_flow.py:69](../../tests/integration/test_intervention_flow.py)),
  used ~10× across the intervention/F6 suites; plus `time.sleep(2)` after
  compose start ([conftest.py:83](../../tests/integration/conftest.py)).
- **Condition polling** — the hot paths already poll rather than sleep
  (`wait_for_any_event`, `_wait_for_sensitivity_factor`, lobby-color polls at
  0.2–0.3 s intervals), so they cost exactly what the system takes.

A Go or k6 harness issuing the same RPCs against the same stack waits the
same wall-clock time. The only Python-specific costs are venv sync (a few
seconds, cached) and interpreter startup (~1 s) — noise.

### 1.2 Toolchain lead time per approach

A fair objection: "Python setup always takes time; k6 is just an image."
Measured from the same CI run, the Python toolchain lead is:

| Step | Measured |
|---|---|
| `setup-uv` action (download uv 0.11.19 + GHA cache restore) | 1.4 s |
| venv create + `uv sync` (`Installed 31 packages in 26ms`, warm cache) | ~2 s |
| **Total Python lead per CI run** | **~3.5 s** (~1 % of the job) |

The same comparison for the alternatives, warm vs cold:

| | Per run (warm cache) | Cold cache / first run | Moving parts |
|---|---|---|---|
| Python (uv) | ~3.5 s | ~30–60 s (resolve + download grpcio et al.) | uv version, lockfiles, venv |
| Go | a few seconds (GHA module/build cache) | minutes (module download + first compile of grpc-go) | toolchain version, module cache, build cache |
| k6 | ~0 s (binary already pulled) | one image/binary pull (~tens of MB) | k6 version pin, plus the outer shell scripts it requires (§5) |

k6 genuinely wins this dimension — a static binary with zero dependency
resolution — and Go is the *worst* cold-start of the three. But two things
cap the win at noise level: uv has already shrunk Python's lead to seconds
(this is not the pip-era minute-long install), and k6's missing fixture
layer means an outer shell harness whose own lead time (compose driving,
flag-file mutation scripts) replaces what it saved. A ~3.5 s saving cannot
justify a rewrite when the same job spends ~50 s starting containers and
~270 s waiting on game pacing.

## 2. The gRPC surface the harness must drive

What a replacement harness has to reimplement, from
[helpers.py](../../tests/integration/helpers.py) (~57 KB) plus ~67 KB of test
logic:

| Capability | Mechanism |
|---|---|
| Event collection | background task consuming `StreamGameEvents` (server-stream), filtered by `game_id` |
| Menu-flow driving | `StartMenu`/`ProcessInput`/`StreamMenuEvents` |
| Mock controller control | unary RPCs on port 50062 (`AddControllers` with `reserved`/`tag`, `SimulateButton/Death`, `GetColor`) |
| Headless game starts | `StreamGameEvents(start_config)` + `headless_cleanup` fixture ([conftest.py:216](../../tests/integration/conftest.py)) |
| Flag mutation tests | snapshot/mutate/restore of `services/flagd/*.json` + live OpenFeature RPC evaluation |
| Game-mode end strategies | ~10 per-mode kill choreographies (who dies when, invincibility-aware) |

Bidirectional streams (`StreamButtonEvents`, `StreamGameplayData`) exist on
the surface but the tests currently drive mocks via unary simulate-RPCs; the
load-bearing pattern is the **stateful background collector + imperative
assert** loop.

## 3. Option A — keep Python (status quo and its ceiling)

**What it is today:** pytest-asyncio + testcontainers
([conftest.py](../../tests/integration/conftest.py)), grpc.aio stubs from
`make protos`, session-scoped compose stack, function-scoped cleanup
fixtures.

**Genuine strengths in this codebase:**

- Same language as the four services under test — test authors read
  coordinator code and harness code without switching context; failure
  triage spans both.
- The fixture model maps exactly onto the suite's needs: session compose
  stack, per-test `ensure_game_stopped` / `headless_cleanup` / flag-file
  snapshot-restore. This is the part k6 lacks entirely and Go reimplements
  manually (§5, §6).
- The hard-won choreography (end strategies, exactly-once intervention
  semantics, settle/poll discipline) is already written, reviewed and green.

**Its real weaknesses, honestly:**

- A venv (`clean-test-venv`, [Makefile](../../Makefile)) must be synced per
  run — seconds, but a moving part Go's static binary wouldn't have.
- `testcontainers` + `pytest-asyncio` fixtures have a learning curve; the
  41→57 KB `helpers.py` growth shows the harness accretes.
- No type-checked stubs at edit time unless mypy is wired up (grpc generated
  code has stubs available via `mypy-protobuf`, not currently used).

**Headroom without leaving Python** (evidence the ceiling is high, not a
work plan): the ~10 fixed `RELOAD_SETTLE` sleeps are deletable in favor of
the polls that already follow them (~15–20 s); the CI compose profile
carries a 6-service observability stack
(jaeger/otel-collector/loki/victoria-metrics/prometheus/grafana) that no app
service functionally depends on ([docker-compose.yml](../../docker-compose.yml)
`depends_on` graphs) — profile-gating it cuts startup and runner contention;
and parallelization via headless games (§7) is a `pytest-xdist`/asyncio-
gather step away now that #779 landed.

## 4. Option B — rewrite in Go

**What the setup would look like:** Go stubs already generate via
[buf.gen.go.yaml](../../proto/buf.gen.go.yaml) (`make protos-go`, used by
[connect-proxy](../../services/connect-proxy) and the
[agent](../../services/agent)). The suite would be a `tests/integration-go`
module using
[testcontainers-go's compose module](https://golang.testcontainers.org/features/docker_compose/)
(Docker Compose v2 native, per-service `WaitForService` wait strategies),
table-driven lifecycle tests, goroutine-based event collectors over
`StreamGameEvents`, and `t.Parallel()` for concurrency.

**Genuine advantages:**

- **No venv, no interpreter** — a compiled test binary; the
  `clean-test-venv`/`uv sync` step disappears. Saving: seconds per run.
- **Compile-time typed stubs** — message-shape errors caught at build, not
  at test runtime.
- **First-class parallel runner** — `t.Parallel()` + `-parallel` is built in
  ([go docs](https://pkg.go.dev/testing)); no plugin needed.
- **Convergence with the Go agent**: the agent already maintains Go clients
  for game-coordinator/controller-manager, and a Go `ShadowGameRunner`
  (#778) is planned ([774-shadow-games.md §6](774-shadow-games.md)). A Go
  test suite could eventually share that client layer instead of
  maintaining a parallel Python one.

**Honest costs:**

- **~124 KB of harness + test logic re-written from scratch** — the end
  strategies, menu choreography, flag snapshot/restore, exactly-once
  intervention assertions. This code encodes a year of behavioral
  knowledge; porting it is where the bugs come from.
- **Zero effect on the measured bottlenecks** — compose startup and system
  pacing are identical from Go. The suite would still take ~5 min serial.
- **Two-language test maintenance** during any transition; unit tests stay
  Python (they live inside each service), so the "one language" benefit
  never fully materializes.
- testcontainers-go compose is solid but has its own quirks (container
  naming `<service>-1` vs wait strategies — upstream issues #374/#241).
- Parallelism via `t.Parallel()` hits exactly the same coordinator
  constraint as pytest (§7) — Go does not unlock anything Python can't do.

**Net:** Go is the credible alternative, and the agent-convergence argument
is real but *future* — #778 hasn't shipped, and #779 explicitly kept the
test suite in Python. Rewriting 25 tests to save seconds of venv churn fails
any cost/benefit test today.

## 5. Option C — k6

The user's framing was right: adopting k6 is not a harness swap, **the whole
setup changes**. What that means concretely, against current k6 docs:

**Capability check (current, verified):**

- gRPC streaming — including bidirectional — is stable in `k6/net/grpc`
  since **v0.49.0**
  ([release notes](https://github.com/grafana/k6/releases/tag/v0.49.0)); the
  old "k6 can't do bidi" objection is outdated. The API is event-callback
  based: `stream.on('data'|'error'|'end', cb)`, `stream.write()`, no
  async/await on streams
  ([Stream docs](https://grafana.com/docs/k6/latest/javascript-api/k6-net-grpc/stream/)).
- Proto loading is dynamic: `client.load(.proto)`, compiled descriptor sets,
  or server reflection — always plain JS objects marshalled via protojson,
  **no codegen, no type safety**
  ([gRPC docs](https://grafana.com/docs/k6/latest/using-k6/protocols/grpc/)).
- TypeScript is supported natively since v0.57, but type-stripping only — no
  checking
  ([compatibility-mode docs](https://grafana.com/docs/k6/latest/using-k6/javascript-typescript-compatibility-mode/)).
- Functional testing is documented and possible
  ([functional testing example](https://grafana.com/docs/k6/latest/examples/functional-testing/)),
  but the assertion model is load-shaped: `check()` does **not** fail the
  run by default — pass/fail must be wired through thresholds
  (`checks: ['rate==1.00']`, `abortOnFail`) or explicit `fail()`
  ([thresholds docs](https://grafana.com/docs/k6/latest/using-k6/thresholds/)).

**What the suite would become:**

- JS/TS scenario scripts per test group; per-VU init loading protos; the
  imperative `await collector.wait_for(...)` assertions re-expressed as
  callback state machines over `stream.on('data')` plus `sleep()`-based flow
  control. The stateful exactly-once intervention assertions are the
  worst-case fit for this model.
- **No fixture system.** k6 has `setup()`/`teardown()` once per script —
  nothing like per-test `headless_cleanup` or flag-file snapshot/restore.
  All of that moves to an outer orchestration layer (Makefile/shell driving
  compose, mutating `services/flagd/*.json` between k6 invocations), i.e. a
  second harness *around* k6.
- Compose lifecycle, GHCR pulls, dev mounts: unchanged — k6 saves zero
  setup time.

**Where k6 would genuinely win:** if the goal were *load* — hundreds of VUs
each running a shadow game via the §7 headless API, latency thresholds,
soak runs — k6's scenario/VU model and metrics pipeline (which the repo's
Grafana stack could ingest directly) are exactly right, and the existing
suite would be terrible at it. **But load testing is explicitly out of
scope for this decision.** For 25 stateful functional tests, k6 replaces a
fitting tool with a mis-fitting one and adds an orchestration shell on top.

## 6. Parallelization compared

The cross-cutting insight: **serial execution was never a property of the
test language.** The game-coordinator held exactly one game; every harness
in every language had to force-end between tests. The shadow-games work
removed that constraint at the server (#775 multi-session, #776 `game_id`
routing, #777 reserved mocks), and #779 already shipped the test-side
machinery: `start_game_headless()`, `game_id`-filtered collectors,
`headless_cleanup`, and
[test_concurrent_games.py](../../tests/integration/test_concurrent_games.py)
proving two games run concurrently on one stack.

| | Python | Go | k6 |
|---|---|---|---|
| Mechanism | `pytest-xdist` or `asyncio.gather` over headless starts | `t.Parallel()` | scenarios / VUs |
| Unlocked by | #775/#776/#779 (done) | same | same |
| Must stay serial | menu-flow tests (single lobby), flag-file mutation tests (shared files) | same | same |
| Realistic gain | lifecycle suite ~125 s → ≈ slowest mode (~30 s) | same | same |

All three converge on the same number because the limit is the system, not
the runner. Choosing Go or k6 *for parallelism* buys nothing Python doesn't
already have — the remaining Python work is wiring xdist groups (or an
asyncio-gather parametrization) around the already-existing headless
helpers, with the menu/flag-mutation tests pinned to one worker.

## 7. Decision matrix

| Criterion | Python (keep) | Go (rewrite) | k6 (adopt) |
|---|---|---|---|
| CI wall-clock impact | baseline; headroom via compose-trim/sleep-removal | ≈ baseline (saves venv seconds) | ≈ baseline; adds outer shell |
| Toolchain lead time (§1.2) | ~3.5 s warm / ~1 min cold | seconds warm / minutes cold (compile) | ~0 s warm / one image pull cold ✓ |
| Local feedback loop | baseline; reuse-stack target possible | same constraint (compose) | same constraint + shell |
| Execution time | poll-driven already; xdist unlock available | identical waits | identical waits, clumsier assertions |
| Harness DX | fixtures fit the problem; asyncio learning curve | typed stubs, manual fixture-equivalents | callback streams, no fixtures, `check()` footgun |
| Type safety | runtime (mypy-protobuf possible) | compile-time ✓ | none |
| Rewrite cost | 0 | ~124 KB of behavioral logic | rewrite **plus** new orchestration layer |
| Parallelization ceiling | same | same | same |
| Ecosystem fit | matches the 4 Python services | matches agent/connect-proxy | matches the Grafana observability stack (load use case only) |
| Load testing (out of scope) | poor | good | excellent |

## 8. Recommendation

**Keep Python.** The evidence is one-sided for the current suite: every
identified cost (image build/pull, 13-container startup, system pacing,
flagd settles) is independent of harness language, the fixture model is the
best fit of the three for stateful per-test cleanup, and the only structural
speedup — concurrent headless games — is already unlocked server-side and is
a small Python-side step, not a rewrite. A Go rewrite buys compile-time
types and venv-free runs at the price of re-encoding ~124 KB of behavioral
knowledge; k6 additionally requires an orchestration shell and bends a
load-testing assertion model around functional tests it isn't shaped for.

## 9. When to revisit (falsifiable triggers)

Reopen the Go question if **any** of these becomes true:

1. **Load/soak testing becomes a goal** — then add k6 *alongside* (not
   replacing) the functional suite, driving headless games via the §6
   machinery; that is k6's actual home turf.
2. **The Go `ShadowGameRunner` (#778) ships** and the Python harness starts
   duplicating its game-driving client logic — reuse-from-`go test` then has
   a concrete payoff instead of a hypothetical one.
3. **The suite grows ~10×** (mode × backend matrices, agent-scenario
   batteries) such that compile-time safety and `go test` ergonomics
   amortize a port.
4. **Profiling shows harness overhead dominating** after the
   language-independent costs are addressed — currently false by two orders
   of magnitude.

If none hold, the cheapest speed wins remain inside the current stack:
delete the settle-sleeps in favor of their existing polls, profile-gate the
observability containers out of CI, and parallelize the lifecycle matrix
over headless games.

## 10. References

- Suite: [conftest.py](../../tests/integration/conftest.py),
  [helpers.py](../../tests/integration/helpers.py),
  [test_full_game_lifecycle.py](../../tests/integration/test_full_game_lifecycle.py),
  [test_intervention_flow.py](../../tests/integration/test_intervention_flow.py),
  [test_f6_flow.py](../../tests/integration/test_f6_flow.py),
  [test_concurrent_games.py](../../tests/integration/test_concurrent_games.py)
- Infra: [docker-compose.yml](../../docker-compose.yml),
  [docker-compose.ci.yml](../../docker-compose.ci.yml),
  [.github/workflows/ci.yml](../../.github/workflows/ci.yml),
  [Makefile](../../Makefile)
- Codegen: [generate_proto.sh](../../proto/generate_proto.sh) (Python),
  [buf.gen.go.yaml](../../proto/buf.gen.go.yaml) (Go)
- Prior research: [774-shadow-games.md](774-shadow-games.md)
- Upstream: [k6 gRPC](https://grafana.com/docs/k6/latest/using-k6/protocols/grpc/),
  [k6 v0.49.0 release](https://github.com/grafana/k6/releases/tag/v0.49.0),
  [k6 thresholds](https://grafana.com/docs/k6/latest/using-k6/thresholds/),
  [testcontainers-go compose](https://golang.testcontainers.org/features/docker_compose/),
  [go testing](https://pkg.go.dev/testing)
- Measured run: CI run 27028616936 (2026-06-05, `main`), integration job:
  `25 passed in 325.55s`, job total ~5 m 41 s.
